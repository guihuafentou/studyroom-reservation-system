# -*- coding: utf-8 -*-
"""
座位占用检测器
核心设计：两类背景建模，按视频源特点选择（config.json detector 字段）：
  1. mog2：MOG2 混合高斯 + 冻结背景（learningRate=0）。
     适用于"可空场初始化"的摄像头场景：背景基准在空场时建立后不再更新，
     任何与空背景存在差异的目标（静坐的人、占座的书包水杯）都持续显示为前景，
     匹配"治理占座"目标。
  2. median：滑动统计背景 + 帧差阈值。
     适用于"无纯空场、人员动态变化"的视频素材（如答辩演示视频）：
     周期性用最近窗口帧逐像素中值重算背景，抹掉短暂就座/路过的人影，
     同时捕捉人员的持续微动（打字/玩手机/转身）与坐下/离开动作。
  3. yolo：YOLOv8n ONNX（COCO 预训练）目标检测。
     启动时自动识别"哪些是座位"（chair 聚类 + 坐姿 person 补座），
     在线用 person 与座位框重叠判定占用，无需手工标定 config seats。

检测流程（公共部分）：
  帧 → 缩放 → 灰度 → 背景差分得到前景掩码
     → 形态学开闭运算 → 逐座位 ROI 统计前景像素占比 → 防抖状态机
线程安全：基类用模板方法统一加锁（RLock 可重入），子类只实现无锁的
_detect_impl / _reset_impl，/detect 与 /video_feed 并发调用同一实例安全。
"""
import collections
import json
import logging
import os
import shutil
import tempfile
import threading
import time
from abc import ABC, abstractmethod

import cv2
import numpy as np

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

WORK_WIDTH = 640            # 统一缩放宽度，ROI 坐标基于该尺寸标注
BG_LEARN_RATE = 0.03        # 背景模型学习收敛率（空场初始化阶段）
MIN_REFRESH_SAMPLES = 10    # 中值重算所需最小缓冲帧数
BLUR_KERNEL = (5, 5)        # 高斯滤波核
MORPH_KERNEL = (5, 5)       # 形态学核
CONFIDENCE_ROUND_DIGITS = 2  # 置信度保留小数位
SHADOW_THRESHOLD = 200       # MOG2 前景掩码二值化阈值：像素>200 判前景，127 灰阶阴影被滤除
RESET_FRAMES_DEFAULT = 40    # 背景初始化默认采样帧数（app.py 与 __main__ 共用）

logger = logging.getLogger("vision.detector")


class BaseDetector(ABC):
    """检测器抽象接口：YOLOv8 实现同一接口即可无缝替换。

    模板方法：detect / reset_background 由基类持锁并调用子类无锁实现，
    保证 /detect 与 /video_feed 并发调用时的共享状态一致。"""

    name = "base"
    supports_background_reset = False  # 默认不支持背景重置；背景建模类子类显式置 True

    def __init__(self, cfg: dict):
        # 参数优先级：检测器段（如 median）> base 段（跨检测器公共默认）> 代码默认值
        self._base_cfg = cfg.get("base") or {}
        self._det_cfg = cfg.get(self.name) or {}
        self.occupy_ratio = self._read_param("occupy_ratio", 0.1, float,
                                              positive=True, min_val=1e-6, max_val=0.999999)
        self.debounce_frames = self._read_param("debounce_frames", 5, int, strict_int=True)
        if self.debounce_frames < 1:
            raise ValueError("debounce_frames 必须 >= 1")

        # 座位 ROI：seat_no -> [x1, y1, x2, y2]（闭区间，基于 WORK_WIDTH 工作域标注）
        self.seats = {}
        for s in cfg.get("seats") or []:
            seat_no = s.get("seat_no")
            bbox = s.get("bbox")
            if seat_no is None or str(seat_no) == "":
                logger.warning("座位配置缺少 seat_no，已忽略: %r", s)
                continue
            if not (isinstance(bbox, (list, tuple)) and len(bbox) == 4):
                logger.warning("座位 %s 的 bbox 非法（须为长度 4 的列表），已忽略: %r", seat_no, bbox)
                continue
            try:
                x1, y1, x2, y2 = (int(v) for v in bbox)
            except (TypeError, ValueError):
                logger.warning("座位 %s 的 bbox 含非数值，已忽略: %r", seat_no, bbox)
                continue
            if x1 >= x2 or y1 >= y2:
                logger.warning("座位 %s 的 bbox 坐标倒置（x1>=x2 或 y1>=y2），已忽略: %r", seat_no, bbox)
                continue
            self.seats[str(seat_no)] = [x1, y1, x2, y2]

        self.background_ready = False
        # 防抖状态机: seat_no -> {"state": bool, "trend": bool, "counter": int}
        self._debounce = {}
        self.last_result = {}
        self._lock = threading.RLock()
        # 形态学核一次性创建并缓存（逐帧重建属无谓开销）
        self._morph_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, MORPH_KERNEL)

    # ---------------- 配置读取（统一校验） ----------------

    def _read_param(self, key, default, coerce, strict_int=False, positive=False,
                    min_val=None, max_val=None):
        v = self._det_cfg.get(key, self._base_cfg.get(key, default))
        if v is None:
            v = default  # JSON null 视为"未配置"，回落代码默认值
        if strict_int and not (isinstance(v, int) and not isinstance(v, bool)):
            raise ValueError("配置项 %s.%s 必须为整数，当前: %r" % (self.name, key, v))
        if coerce is bool:
            if not isinstance(v, bool):
                raise ValueError("配置项 %s.%s 必须为布尔值，当前: %r" % (self.name, key, v))
            return v
        if isinstance(v, bool):
            # 非布尔字段收到布尔值同样拒绝（float(True)==1.0 会绕过正数/区间校验）
            raise ValueError("配置项 %s.%s 必须为数值，不能是布尔值，当前: %r" % (self.name, key, v))
        try:
            result = coerce(v)
        except (TypeError, ValueError):
            raise ValueError("配置项 %s.%s 非法: %r" % (self.name, key, v))
        if positive and result <= 0:
            raise ValueError("配置项 %s.%s 必须为正数，当前: %r" % (self.name, key, v))
        if min_val is not None and result < min_val:
            raise ValueError("配置项 %s.%s 必须 >= %s，当前: %r" % (self.name, key, min_val, v))
        if max_val is not None and result > max_val:
            raise ValueError("配置项 %s.%s 必须 <= %s，当前: %r" % (self.name, key, max_val, v))
        return result

    # ---------------- 模板方法（持锁） ----------------

    def reset_background(self, frames) -> bool:
        if not self.supports_background_reset:
            # 无背景类检测器（如 YOLO）不支持背景重置，统一在此拦截
            return False
        with self._lock:
            ok = self._reset_impl(frames)
            if ok:
                # 背景重建成功后清理防抖状态与最近结果，避免"重置后仍显示占用"假象
                self._debounce.clear()
                self.last_result.clear()
            return ok

    def detect(self, frame) -> dict:
        """模板方法：就绪守卫 → 预处理（灰度+BGR）→ 子类帧级判定 → 返回结果拷贝（防别名污染）"""
        with self._lock:
            if not self.background_ready:
                return {}
            gray = self._preprocess(frame)
            if gray is None:
                return {}
            bgr = self._preprocess_bgr(frame)
            final = self._detect_frame(gray, bgr)
            return {k: dict(v) for k, v in final.items()}

    def get_last_result(self) -> dict:
        """持锁逐座位拷贝最近结果（外层与内层 dict 均为新建，供视频渲染线程读取）"""
        with self._lock:
            return {k: dict(v) for k, v in self.last_result.items()}

    def _reset_impl(self, frames) -> bool:
        """背景类检测器覆写此方法；目标检测类（如 YOLO）无需背景，继承默认 no-op。"""
        return False

    @abstractmethod
    def _detect_gray(self, gray) -> dict:
        """子类实现：输入已预处理灰度工作帧，
        返回 {seat_no: {occupied, confidence, valid}}；防抖与结果缓存由基类 _verdict 统一完成。"""
        raise NotImplementedError

    def _detect_frame(self, gray, bgr):
        """帧级判定入口：默认仅用灰度，委托给 _detect_gray；
        需要色彩/外观互补判据的子类（median）覆写本方法。"""
        return self._detect_gray(gray)

    # ---------------- 工具（公共） ----------------

    @staticmethod
    def _preprocess(frame):
        """缩放 + 灰度，返回灰度工作帧；异常输入返回 None。"""
        if frame is None:
            return None
        h, w = frame.shape[:2]
        if h <= 0 or w <= 0:
            return None
        scale = WORK_WIDTH / w
        work_h = max(1, int(h * scale))
        resized = cv2.resize(frame, (WORK_WIDTH, work_h))
        if resized.ndim == 2:
            gray = resized
        elif resized.ndim == 3 and resized.shape[2] == 3:
            gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
        elif resized.ndim == 3 and resized.shape[2] == 4:
            gray = cv2.cvtColor(resized, cv2.COLOR_BGRA2GRAY)
        else:
            return None
        gray = cv2.GaussianBlur(gray, BLUR_KERNEL, 0)
        return gray

    @staticmethod
    def _preprocess_bgr(frame):
        """缩放为 WORK_WIDTH 工作域的 BGR 帧，供需色彩信息的判据（肤色）使用。"""
        if frame is None:
            return None
        h, w = frame.shape[:2]
        if h <= 0 or w <= 0:
            return None
        scale = WORK_WIDTH / w
        work_h = max(1, int(h * scale))
        resized = cv2.resize(frame, (WORK_WIDTH, work_h))
        if resized.ndim == 3 and resized.shape[2] == 3:
            return resized
        if resized.ndim == 2:
            return cv2.cvtColor(resized, cv2.COLOR_GRAY2BGR)
        if resized.ndim == 3 and resized.shape[2] == 4:
            return cv2.cvtColor(resized, cv2.COLOR_BGRA2BGR)
        return None

    @staticmethod
    def _roi_rect(bbox, frame_w, frame_h):
        """ROI 裁剪边界：bbox 为闭区间标注，返回 Python 切片开区间右界（+1）。"""
        x1, y1, x2, y2 = bbox
        x1 = max(x1, 0); y1 = max(y1, 0)
        x2 = min(x2, frame_w - 1) + 1
        y2 = min(y2, frame_h - 1) + 1
        if x1 >= x2 or y1 >= y2:
            return None
        return (x1, y1, x2, y2)

    def _apply_morphology(self, mask: np.ndarray) -> np.ndarray:
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self._morph_kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, self._morph_kernel)
        return mask

    def _debounce_apply(self, raw: dict) -> dict:
        """防抖状态机：连续 debounce_frames 帧同向才翻转对外状态（state），抑制单帧噪声。"""
        final = {}
        for seat_no, r in raw.items():
            if not r.get("valid", True):
                self._debounce.pop(seat_no, None)
                final[seat_no] = {"occupied": False, "confidence": r.get("confidence", 0.0),
                                  "valid": False}
                continue
            occ = r["occupied"]
            d = self._debounce.setdefault(
                seat_no, {"state": False, "trend": False, "counter": 0})
            if occ == d["trend"]:
                d["counter"] += 1
            else:
                d["trend"] = occ
                d["counter"] = 1
            if d["counter"] >= self.debounce_frames:
                d["state"] = occ
                d["counter"] = self.debounce_frames
            final[seat_no] = {"occupied": d["state"], "confidence": r["confidence"],
                              "valid": r.get("valid", True)}
        self.last_result = final
        return final

    def _verdict(self, fgmask: np.ndarray) -> dict:
        """逐座位 ROI 统计前景像素占比 → 阈值判定（防抖在 _debounce_apply 完成）"""
        if fgmask is None or fgmask.size == 0:
            return {}
        frame_h = fgmask.shape[0]
        frame_w = fgmask.shape[1]
        result = {}
        for seat_no, bbox in self.seats.items():
            rect = self._roi_rect(bbox, frame_w, frame_h)
            if rect is None:
                result[seat_no] = {"occupied": False, "confidence": 0.0, "valid": False}
                continue
            x1, y1, x2, y2 = rect
            roi_mask = fgmask[y1:y2, x1:x2]
            area = (x2 - x1) * (y2 - y1)
            fg_count = cv2.countNonZero(roi_mask)
            ratio = fg_count / area
            occupied = ratio >= self.occupy_ratio
            if occupied:
                confidence = min((ratio - self.occupy_ratio) / max(1.0 - self.occupy_ratio, 1e-6), 1.0)
            else:
                confidence = max(1.0 - ratio / self.occupy_ratio, 0.0)
            result[seat_no] = {"occupied": occupied,
                               "confidence": round(confidence, CONFIDENCE_ROUND_DIGITS),
                               "valid": True}
        return self._debounce_apply(result)


class Mog2Detector(BaseDetector):
    name = "mog2"
    supports_background_reset = True

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.history = self._read_param("history", 200, int, strict_int=True, positive=True)
        self.var_threshold = self._read_param("var_threshold", 16, float, positive=True)
        self.detect_shadows = self._read_param("detect_shadows", False, bool)
        self.learning_rate = self._read_param("learning_rate", 0.0, float,
                                              min_val=0.0, max_val=1.0)
        self.bg_learn_rate = self._read_param("bg_learn_rate", BG_LEARN_RATE, float,
                                              positive=True, max_val=1.0)
        self.bg_sub = cv2.createBackgroundSubtractorMOG2(
            history=self.history, varThreshold=self.var_threshold,
            detectShadows=self.detect_shadows)

    def _reset_impl(self, frames) -> bool:
        """空场初始化背景：重建模型后，用多帧真实画面学习背景，完成后冻结。"""
        if not frames:
            self.background_ready = False
            return False
        self.bg_sub = cv2.createBackgroundSubtractorMOG2(
            history=self.history, varThreshold=self.var_threshold,
            detectShadows=self.detect_shadows)
        learned = 0
        for frame in frames:
            gray = self._preprocess(frame)
            if gray is None:
                continue
            self.bg_sub.apply(gray, learningRate=self.bg_learn_rate)
            learned += 1
        if learned == 0:
            self.background_ready = False
            return False
        self.background_ready = True
        return True

    def _detect_gray(self, gray) -> dict:
        fgmask = self.bg_sub.apply(gray, learningRate=self.learning_rate)
        fgmask = cv2.threshold(fgmask, SHADOW_THRESHOLD, 255, cv2.THRESH_BINARY)[1]
        fgmask = self._apply_morphology(fgmask)
        return self._verdict(fgmask)


class MedianDetector(BaseDetector):
    """滑动统计背景 + 帧差阈值。
    周期性用最近窗口帧逐像素中值重算背景：
      - 抹掉短暂就座/路过的人影（窗口内占比<50%的目标不进入中值）；
      - 捕捉人员的持续微动（打字/玩手机/转身）与坐下/离开动作；
      - 无需纯空场，适配无空场、人员动态变化的监控素材。"""

    name = "median"
    supports_background_reset = True

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.diff_threshold = self._read_param("diff_threshold", 30, float, positive=True)
        self._size_warned = False
        self.window_frames = self._read_param("window_frames", 90, int,
                                              strict_int=True, positive=True)
        self.refresh_frames = self._read_param("refresh_frames", 30, int,
                                               strict_int=True, positive=True)
        self.freeze_bg = self._read_param("freeze_bg", False, bool)
        self.bg_image = None
        self._gray_buf = collections.deque(maxlen=self.window_frames)
        self._frame_count = 0

        # 互补外观判据（解决"背景中值≈静止伏案者 → 差分≈0"的漏检）：
        # 肤色先验（YCbCr Chai-Ngan 经典区间）+ 高频纹理密度（Laplacian 方差），
        # 与背景差分 OR 融合，任一判据命中即判占用。
        self.appearance_enabled = self._read_param("appearance_enabled", True, bool)
        self.skin_cb_min = self._read_param("skin_cb_min", 77, int,
                                            strict_int=True, min_val=0, max_val=255)
        self.skin_cb_max = self._read_param("skin_cb_max", 127, int,
                                            strict_int=True, min_val=0, max_val=255)
        self.skin_cr_min = self._read_param("skin_cr_min", 133, int,
                                            strict_int=True, min_val=0, max_val=255)
        self.skin_cr_max = self._read_param("skin_cr_max", 173, int,
                                            strict_int=True, min_val=0, max_val=255)
        self.skin_ratio_thr = self._read_param("skin_ratio_thr", 0.08, float,
                                               positive=True, max_val=1.0)
        self.tex_norm = self._read_param("tex_norm", 500.0, float, positive=True)
        self.tex_score_thr = self._read_param("tex_score_thr", 0.30, float,
                                              positive=True, max_val=1.0)
        self._skin_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))

    def _median_bg(self, grays) -> "np.ndarray | None":
        if not grays:
            return None
        ref = grays[0].shape
        aligned = [g if g.shape == ref else cv2.resize(g, (ref[1], ref[0])) for g in grays]
        return np.rint(np.median(np.stack(aligned), axis=0)).astype(np.uint8)

    def _reset_impl(self, frames) -> bool:
        """初始化统计背景：清空旧缓冲，多帧灰度入缓冲并对整个缓冲取中值"""
        if not frames:
            self.background_ready = False
            self.bg_image = None
            return False
        self._gray_buf = collections.deque(maxlen=self.window_frames)
        self._frame_count = 0
        for frame in frames:
            gray = self._preprocess(frame)
            if gray is None:
                continue
            self._gray_buf.append(gray)
        if len(self._gray_buf) < MIN_REFRESH_SAMPLES:
            logger.warning("背景初始化样本不足（%d < %d），保持未就绪",
                           len(self._gray_buf), MIN_REFRESH_SAMPLES)
            self.background_ready = False
            self.bg_image = None
            return False
        self.bg_image = self._median_bg(self._gray_buf)
        self.background_ready = True
        return True

    def _refresh_background(self):
        if len(self._gray_buf) < MIN_REFRESH_SAMPLES:
            return
        self.bg_image = self._median_bg(self._gray_buf)

    def _compute_fgmask(self, gray):
        """背景差分公共段：维护缓冲/背景并返回前景掩码；背景未就绪/样本不足返回 None。"""
        self._gray_buf.append(gray)
        self._frame_count += 1
        if not self.freeze_bg and self._frame_count % self.refresh_frames == 0:
            self._refresh_background()
        bg = self.bg_image
        if bg is None or bg.shape != gray.shape:
            if len(self._gray_buf) >= MIN_REFRESH_SAMPLES:
                self.bg_image = self._median_bg(list(self._gray_buf))
                bg = self.bg_image
            if bg is None or bg.shape != gray.shape:
                if not self._size_warned:
                    self._size_warned = True
                    logger.warning("Median 背景尺寸与当前帧不一致且样本不足，返回空结果（等待重算）")
                return None
        diff = cv2.absdiff(gray, bg)
        _, fgmask = cv2.threshold(diff, self.diff_threshold, 255, cv2.THRESH_BINARY)
        return self._apply_morphology(fgmask)

    def _detect_gray(self, gray) -> dict:
        """纯差分路径（外观判据关闭、或输入无 BGR 帧时的回退）"""
        fgmask = self._compute_fgmask(gray)
        if fgmask is None:
            return {}
        return self._verdict(fgmask)

    def _detect_frame(self, gray, bgr) -> dict:
        """median 帧级判定：差分 + 肤色/纹理外观判据 OR 融合。"""
        if not self.appearance_enabled or bgr is None:
            return self._detect_gray(gray)
        fgmask = self._compute_fgmask(gray)
        if fgmask is None:
            return {}
        return self._verdict_fused(fgmask, gray, bgr)

    def _verdict_fused(self, fgmask, gray, bgr) -> dict:
        """差分占比 + 肤色占比 + 纹理占比融合判占：
        strength = max(差分强度, 肤色强度, 纹理强度)，任一判据强度 ≥1 即判占用。"""
        frame_h = fgmask.shape[0]
        frame_w = fgmask.shape[1]
        result = {}
        for seat_no, bbox in self.seats.items():
            rect = self._roi_rect(bbox, frame_w, frame_h)
            if rect is None:
                result[seat_no] = {"occupied": False, "confidence": 0.0, "valid": False}
                continue
            x1, y1, x2, y2 = rect
            roi_mask = fgmask[y1:y2, x1:x2]
            area = (x2 - x1) * (y2 - y1)
            diff_ratio = cv2.countNonZero(roi_mask) / area
            skin_ratio, tex_ratio = self._appearance_score(gray, bgr, (x1, y1, x2, y2))
            diff_strength = diff_ratio / self.occupy_ratio
            skin_strength = skin_ratio / self.skin_ratio_thr
            tex_strength = tex_ratio / self.tex_score_thr
            strength = max(diff_strength, skin_strength, tex_strength)
            occupied = strength >= 1.0
            if occupied:
                confidence = min(strength / (strength + 1.0), 0.95)
            else:
                confidence = max(1.0 - strength, 0.0)
            result[seat_no] = {"occupied": occupied,
                               "confidence": round(confidence, CONFIDENCE_ROUND_DIGITS),
                               "valid": True}
        return self._debounce_apply(result)

    def _appearance_score(self, gray, bgr, rect):
        """返回 (skin_ratio, tex_ratio) 两个归一化外观占比（0..1）。"""
        x1, y1, x2, y2 = rect
        roi_gray = gray[y1:y2, x1:x2]
        roi_bgr = bgr[y1:y2, x1:x2]
        area = roi_gray.size
        if area == 0:
            return 0.0, 0.0
        ycrcb = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2YCrCb)
        cr = ycrcb[:, :, 1]
        cb = ycrcb[:, :, 2]
        skin = ((cb >= self.skin_cb_min) & (cb <= self.skin_cb_max) &
                (cr >= self.skin_cr_min) & (cr <= self.skin_cr_max)).astype(np.uint8)
        skin = cv2.morphologyEx(skin, cv2.MORPH_OPEN, self._skin_kernel)
        skin_ratio = float(cv2.countNonZero(skin)) / area
        lap = cv2.Laplacian(roi_gray, cv2.CV_32F)
        tex_ratio = min(float(lap.var()) / self.tex_norm, 1.0)
        return skin_ratio, tex_ratio


class YoloDetector(BaseDetector):
    """YOLOv8n ONNX（COCO 预训练）检测器：自动识别"哪些是座位"。
    - 启动标定（reset_background）：对初始化帧批量检测 chair（COCO 56），
      按中心距离聚类生成稳定的座位集合（自动标定，无需手工 config seats）；
    - 在线判定：检测 person（COCO 0），person 框与座位框重叠/中心落入 → 该座位占用；
    - 仅依赖 cv2.dnn 与模型文件 yolov8n.onnx（12.8MB，可从 ModelScope 下载，见 README）；
    - 推理节流（cache_interval 秒内复用上次结果），保证 MJPEG 流不卡顿。
    """

    name = "yolo"
    supports_background_reset = True  # 语义：座位自动标定

    COCO_CHAIR = 56
    COCO_PERSON = 0

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.model_path = self._read_param("model_path", "models/yolov8n.onnx", str)
        self.conf_threshold = self._read_param("conf_threshold", 0.25, float,
                                               min_val=0.0, max_val=1.0)
        self.iou_threshold = self._read_param("iou_threshold", 0.45, float,
                                              min_val=0.0, max_val=1.0)
        self.min_chair_w = self._read_param("min_chair_w", 20, int, strict_int=True, min_val=4)
        self.min_chair_h = self._read_param("min_chair_h", 20, int, strict_int=True, min_val=4)
        self.max_seats = self._read_param("max_seats", 40, int, strict_int=True, positive=True)
        self.dedup_dist = self._read_param("dedup_dist", 45, int, strict_int=True, positive=True)
        self.occupy_overlap = self._read_param("occupy_overlap", 0.15, float,
                                               min_val=0.0, max_val=1.0)
        self.sit_aspect_min = self._read_param("sit_aspect_min", 0.45, float,
                                               min_val=0.0, max_val=2.0)
        self.person_floor_ratio = self._read_param("person_floor_ratio", 0.55, float,
                                                   min_val=0.2, max_val=0.9)
        self.label_frames = self._read_param("label_frames", 12, int, strict_int=True, positive=True)
        self.cache_interval = self._read_param("cache_interval", 1.0, float, positive=True)
        self._net = None
        self._tmp_model = None  # 中文路径规避：复制到英文临时目录的模型路径
        self._last_infer_ts = 0.0
        self._size_warned = False

    # ---------------- 模型加载（中文路径规避） ----------------

    def _ensure_net(self):
        if self._net is not None:
            return self._net
        mp = self.model_path
        if not os.path.isabs(mp):
            mp = os.path.join(os.path.dirname(os.path.abspath(__file__)), mp)
        if not os.path.exists(mp):
            raise RuntimeError("YOLO 模型文件不存在: %s（请从 ModelScope 下载 yolov8n.onnx 放入 models/）" % mp)
        # cv2.dnn 对含非 ASCII（中文）路径的 ONNX 读取失败：复制到英文临时目录加载
        if any(ord(c) > 127 for c in mp):
            # 按文件名建独立子目录，避免不同模型同名/同大小互相覆盖
            tmp_dir = os.path.join(tempfile.gettempdir(), "vis_models", os.path.splitext(os.path.basename(mp))[0])
            os.makedirs(tmp_dir, exist_ok=True)
            tmp_mp = os.path.join(tmp_dir, os.path.basename(mp))
            if not os.path.exists(tmp_mp) or os.path.getsize(tmp_mp) != os.path.getsize(mp):
                shutil.copy2(mp, tmp_mp)
            mp = tmp_mp
            self._tmp_model = tmp_mp
        self._net = cv2.dnn.readNetFromONNX(mp)
        logger.info("YOLOv8n ONNX 加载完成: %s", mp)
        return self._net

    # ---------------- 推理 ----------------

    @staticmethod
    def _letterbox(img, new=(640, 640)):
        h, w = img.shape[:2]
        r = min(new[0] / h, new[1] / w)
        nh, nw = int(round(h * r)), int(round(w * r))
        resized = cv2.resize(img, (nw, nh))
        canvas = np.full((new[0], new[1], 3), 114, dtype=np.uint8)
        pad_t = (new[0] - nh) // 2
        pad_l = (new[1] - nw) // 2
        canvas[pad_t:pad_t + nh, pad_l:pad_l + nw] = resized
        return canvas, r, pad_t, pad_l

    def _infer(self, bgr):
        """YOLOv8 前向，返回 {cls_id: [(原图坐标 bbox(x1,y1,w,h), score)]}"""
        net = self._ensure_net()
        inp, r, pad_t, pad_l = self._letterbox(bgr)
        blob = cv2.dnn.blobFromImage(inp, 1 / 255.0, (640, 640), swapRB=True)
        net.setInput(blob)
        out = net.forward()[0].transpose(1, 0)  # (8400, 84)
        dets = {self.COCO_PERSON: [], self.COCO_CHAIR: []}
        boxes_all = []
        scores_all = []
        idx_map = []
        for row in out:
            cls_scores = row[4:]
            cls_id = int(np.argmax(cls_scores))
            score = float(cls_scores[cls_id])
            if cls_id not in (self.COCO_PERSON, self.COCO_CHAIR) or score < self.conf_threshold:
                continue
            x, y, w, h = row[0], row[1], row[2], row[3]
            # 关键：w/h 与 x1/y1 一样必须除以 letterbox 缩放比 r，统一回原始坐标域
            x1 = (x - w / 2 - pad_l) / r
            y1 = (y - h / 2 - pad_t) / r
            w /= r
            h /= r
            boxes_all.append([x1, y1, w, h])
            scores_all.append(score)
            idx_map.append(cls_id)
        if boxes_all:
            keep = cv2.dnn.NMSBoxes(boxes_all, scores_all, self.conf_threshold, self.iou_threshold)
            # OpenCV 版本兼容：旧版返回 (N,1) 二维数组，统一拉平为一维索引
            if keep is not None and len(keep) > 0:
                keep = np.asarray(keep).flatten().astype(int)
                for i in keep:
                    cls_id = idx_map[i]
                    dets[cls_id].append((boxes_all[i], scores_all[i]))
        return dets

    # ---------------- 座位自动标定 ----------------

    def _reset_impl(self, frames) -> bool:
        """批量检测 chair → 按中心距离聚类生成稳定座位集合（自动标定）。"""
        self._ensure_net()
        cands = []  # 座位候选 (cx, cy, x, y, w, h)：chair 全量 + 坐姿 person 下半框
        for frame in (frames or [])[:self.label_frames]:
            bgr = self._preprocess_bgr(frame)
            if bgr is None:
                continue
            res = self._infer(bgr)
            for (box, score) in res.get(self.COCO_CHAIR, []):
                x, y, w, h = box
                if w >= self.min_chair_w and h >= self.min_chair_h:
                    cands.append((x + w / 2, y + h / 2, x, y, w, h))
            for (box, score) in res.get(self.COCO_PERSON, []):
                x, y, w, h = box
                if h <= 0 or w < self.min_chair_w or h < self.min_chair_h:
                    continue
                aspect = w / h
                if aspect < self.sit_aspect_min:  # 站姿/走动者不作为座位候选
                    continue
                # 坐姿 person：椅子在框下部区域，取下部 person_floor_ratio 为座位候选
                sh = h * self.person_floor_ratio
                sy = y + h - sh
                cands.append((x + w / 2, sy + sh / 2, x, sy, w, sh))
        if len(cands) < 1:
            logger.warning("YOLO 未检出任何椅子或坐姿人员，座位标定失败（保留原座位基线）")
            self.background_ready = False
            return False
        # 贪心聚类：以簇中心为基准，中心距离 < dedup_dist 并入，否则新簇
        clusters = []  # [{"cx","cy","boxes":[...]}]
        for c in cands:
            cx, cy = c[0], c[1]
            best_i = -1
            best_d = None
            for i, cl in enumerate(clusters):
                d = ((cl["cx"] - cx) ** 2 + (cl["cy"] - cy) ** 2) ** 0.5
                if d < self.dedup_dist and (best_d is None or d < best_d):
                    best_d = d
                    best_i = i
            if best_i >= 0:
                cl = clusters[best_i]
                cl["boxes"].append(c)
                n = len(cl["boxes"])
                cl["cx"] = sum(b[0] for b in cl["boxes"]) / n
                cl["cy"] = sum(b[1] for b in cl["boxes"]) / n
            else:
                clusters.append({"cx": cx, "cy": cy, "boxes": [c]})
        clusters.sort(key=lambda cl: (cl["cy"], cl["cx"]))  # 自上而下、自左而右编号
        # 手工预置座位（config seats）作为基线，自动簇与其去重后合并
        merged = dict(self.seats)
        next_no = 1
        for seat_no in merged:
            try:
                next_no = max(next_no, int(seat_no.split("-")[-1]) + 1)
            except (ValueError, IndexError):
                pass
        for cl in clusters[:self.max_seats]:
            xs1 = [b[2] for b in cl["boxes"]]; ys1 = [b[3] for b in cl["boxes"]]
            xs2 = [b[2] + b[4] for b in cl["boxes"]]; ys2 = [b[3] + b[5] for b in cl["boxes"]]
            bbox = [int(round(min(xs1))), int(round(min(ys1))),
                    int(round(max(xs2))), int(round(max(ys2)))]
            # 去重中心取合并后 bbox 中心（与最终座位框一致，避免簇中心偏差）
            cx, cy = (bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0
            # 与已有座位去重：中心落在已有座位框内则跳过（手工优先）
            if any(bx1 <= cx <= bx2 and by1 <= cy <= by2
                   for (bx1, by1, bx2, by2) in merged.values()):
                continue
            merged["A-%02d" % next_no] = bbox
            next_no += 1
        self.seats = merged
        self.background_ready = True
        self._debounce.clear()
        self.last_result.clear()
        logger.info("YOLO 自动标定座位 %d 个: %s", len(self.seats), list(self.seats)[:8])
        return True

    # ---------------- 占用判定 ----------------

    def _detect_gray(self, gray) -> dict:
        """抽象方法实现：YOLO 判占需要 BGR 帧；灰度输入转 3 通道后复用判占逻辑。"""
        if gray is None:
            return {}
        if len(gray.shape) == 2:
            gray = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        return self._detect_frame(gray, gray)

    def _detect_frame(self, gray, bgr) -> dict:
        if not self.seats:
            return {}
        now = time.time()
        if now - self._last_infer_ts < self.cache_interval and self.last_result:
            # 推理节流：间隔内复用最近结果（MJPEG 逐帧绘制不卡顿）
            return {k: dict(v) for k, v in self.last_result.items()}
        bgr = bgr if bgr is not None else gray
        self._last_infer_ts = now
        res = self._infer(bgr)
        persons = res.get(self.COCO_PERSON, [])
        result = {}
        for seat_no, bbox in self.seats.items():
            x1, y1, x2, y2 = bbox
            sx, sy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
            occupied = False
            best_score = 0.0
            for (pbox, score) in persons:
                px1, py1, pw, ph = pbox
                if ph <= 0:
                    continue
                if pw / ph < self.sit_aspect_min:  # 站姿/走动者不判占用
                    continue
                px2, py2 = px1 + pw, py1 + ph
                ix1, iy1 = max(x1, px1), max(y1, py1)
                ix2, iy2 = min(x2, px2), min(y2, py2)
                inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
                union = (x2 - x1) * (y2 - y1) + (px2 - px1) * (py2 - py1) - inter
                iou = inter / union if union > 0 else 0.0
                center_in = (sx >= px1 and sx <= px2 and sy >= py1 and sy <= py2)
                if iou >= self.occupy_overlap or center_in:
                    if score > best_score:
                        best_score = score
                        occupied = True
            result[seat_no] = {"occupied": occupied,
                               "confidence": round(best_score, CONFIDENCE_ROUND_DIGITS),
                               "valid": True}
        return self._debounce_apply(result)


def load_config(path: str = CONFIG_PATH) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise RuntimeError("配置文件不存在: %s" % path)
    except json.JSONDecodeError as e:
        raise RuntimeError("配置文件 JSON 解析失败 (%s): %s" % (path, e))
    except OSError as e:
        raise RuntimeError("配置文件读取失败 (%s): %s" % (path, e))


DETECTORS = {"mog2": Mog2Detector, "median": MedianDetector, "yolo": YoloDetector}


def create_detector(cfg: dict) -> BaseDetector:
    name = cfg.get("detector", "mog2")
    cls = DETECTORS.get(name)
    if cls is None:
        raise ValueError("不支持的检测器: %s（可用: %s）" % (name, ", ".join(DETECTORS)))
    return cls(cfg)


if __name__ == "__main__":
    # 自测：读取视频源若干帧进行背景初始化与检测
    cfg = load_config()
    det = create_detector(cfg)
    src = cfg.get("video_source", 0)
    cap = cv2.VideoCapture(src)
    ok, frame = cap.read()
    if not ok:
        print("无法读取视频源:", src)
    else:
        reset_n = int(cfg.get(det.name, {}).get("reset_frames", RESET_FRAMES_DEFAULT))
        frames = [frame]
        for _ in range(reset_n - 1):
            ok, f = cap.read()
            if not ok:
                break
            frames.append(f)
        det.reset_background(frames)
        for _ in range(10):
            ok, frame = cap.read()
            if not ok:
                break
            print(det.detect(frame))
    cap.release()

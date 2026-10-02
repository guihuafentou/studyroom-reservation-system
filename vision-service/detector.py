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
    """检测器抽象接口：后续 YOLOv8 实现同一接口即可无缝替换。

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
        # YOLO 推理串行化锁：检测路径"推理区间"与"结果发布区间"分离（不嵌套持锁），
        # 与 reset 路径的 self._lock→_infer_lock 锁序不构成环，防死锁；
        # median/mog2 不用此锁（其 detect() 仍整体持 self._lock）
        self._infer_lock = threading.Lock()
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
            # 无背景类检测器（如 YOLO）不支持背景重置，统一在此拦截，
            # 与 _reset_impl 默认 no-op 的语义保持一致，避免两处行为不一致
            return False
        with self._lock:
            ok = self._reset_impl(frames)
            if ok:
                # 背景重建成功后清理防抖状态与最近结果，避免"重置后仍显示占用"假象。
                # 用整体重绑定替代原地 clear()：无锁迭代 last_result 的线程持有旧 dict
                # 引用，重绑定不改变旧 dict，不会触发 "dictionary changed size" RuntimeError
                self._debounce.clear()
                self.last_result = {}
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
        """背景类检测器覆写此方法；目标检测类（如 YOLO）无需背景，继承默认 no-op。
        返回 False 时基类不清理 _debounce/last_result（无背景类检测器无状态可清）。"""
        return False

    @abstractmethod
    def _detect_gray(self, gray) -> dict:
        """子类实现：输入已预处理灰度工作帧，
        返回 {seat_no: {occupied, confidence, valid}}；防抖与结果缓存由基类 _verdict 统一完成。"""
        raise NotImplementedError

    def _detect_frame(self, gray, bgr):
        """帧级判定入口：默认仅用灰度，委托给 _detect_gray；
        需要色彩/外观互补判据的子类（median）覆写本方法。
        参数 bgr 可能为 None（输入非 BGR 帧时），子类须自行兜底。"""
        return self._detect_gray(gray)

    # ---------------- 工具（公共） ----------------

    @staticmethod
    def _preprocess(frame):
        """缩放 + 灰度，返回灰度工作帧；异常输入返回 None。
        输入帧约定为 BGR（OpenCV VideoCapture 默认色彩空间）。"""
        if frame is None:
            return None
        h, w = frame.shape[:2]
        if h <= 0 or w <= 0:
            return None
        scale = WORK_WIDTH / w
        work_h = max(1, int(h * scale))  # 极端宽高比帧防止目标高度为 0
        resized = cv2.resize(frame, (WORK_WIDTH, work_h))
        if resized.ndim == 2:
            gray = resized  # 单通道灰度帧直接使用
        elif resized.ndim == 3 and resized.shape[2] == 3:
            gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
        elif resized.ndim == 3 and resized.shape[2] == 4:
            gray = cv2.cvtColor(resized, cv2.COLOR_BGRA2GRAY)  # BGRA 取前 3 通道转灰
        else:
            return None
        gray = cv2.GaussianBlur(gray, BLUR_KERNEL, 0)
        return gray

    @staticmethod
    def _preprocess_bgr(frame):
        """缩放为 WORK_WIDTH 工作域的 BGR 帧，供需色彩信息的判据（肤色）使用。
        与 _preprocess 使用同一缩放比例，保证灰度/BGR 两路 ROI 完全对齐；
        不做高斯模糊，保留色彩细节（肤色/纹理判据依赖原始纹理）。"""
        if frame is None:
            return None
        h, w = frame.shape[:2]
        if h <= 0 or w <= 0:
            return None
        scale = WORK_WIDTH / w
        work_h = max(1, int(h * scale))  # 极端宽高比帧防止目标高度为 0
        resized = cv2.resize(frame, (WORK_WIDTH, work_h))
        if resized.ndim == 3 and resized.shape[2] == 3:
            return resized
        if resized.ndim == 2:
            return cv2.cvtColor(resized, cv2.COLOR_GRAY2BGR)
        if resized.ndim == 3 and resized.shape[2] == 4:
            return cv2.cvtColor(resized, cv2.COLOR_BGRA2BGR)  # BGRA 转 BGR 后供肤色判据
        return None

    @staticmethod
    def _roi_rect(bbox, frame_w, frame_h):
        """ROI 裁剪边界：bbox 为闭区间标注（含右/下边界像素），
        返回 Python 切片开区间右界（+1），保证最右/最下列像素被统计。
        若裁剪后宽高非正（ROI 整体越界），返回 None，由调用方跳过该座位。"""
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
        """防抖状态机：跟踪最近判定方向（trend），连续 debounce_frames 帧同向
        才翻转对外状态（state），抑制单帧噪声与瞬时遮挡造成的状态抖动。
        结果发布持短锁：YOLO detect() 推理在锁外，发布与 MJPEG 读取互斥。"""
        with self._lock:
            final = {}
            for seat_no, r in raw.items():
                if not r.get("valid", True):
                    # 不可判定座位（ROI 越界）不参与防抖，直接透传 valid=False，避免被当空闲累计
                    self._debounce.pop(seat_no, None)
                    final[seat_no] = {"occupied": False, "confidence": r.get("confidence", 0.0),
                                      "valid": False}
                    continue
                occ = r["occupied"]
                d = self._debounce.setdefault(
                    seat_no, {"state": False, "trend": False, "counter": 0})
                if occ == d["trend"]:  # trend = 上一帧瞬时占用判定
                    d["counter"] += 1
                else:
                    d["trend"] = occ
                    d["counter"] = 1
                if d["counter"] >= self.debounce_frames:
                    d["state"] = occ
                    d["counter"] = self.debounce_frames
                # confidence 为"当前帧瞬时置信度"（未防抖），occupied 为防抖后状态，
                # 两者语义不同：占用翻转滞后 N 帧，置信度实时反映当前帧
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
                # ROI 越界：标记为不可判定（valid=False），避免被误当"空闲"放行
                result[seat_no] = {"occupied": False, "confidence": 0.0, "valid": False}
                continue
            x1, y1, x2, y2 = rect
            roi_mask = fgmask[y1:y2, x1:x2]
            area = (x2 - x1) * (y2 - y1)
            fg_count = cv2.countNonZero(roi_mask)
            ratio = fg_count / area
            occupied = ratio >= self.occupy_ratio
            # confidence：占用方向强度（占用=超出阈值的程度，空闲=远离阈值的程度）。
            # 业务判定只看 occupied 布尔值，confidence 供可视化/调参参考。
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
    supports_background_reset = True  # 背景建模类：支持背景基准重建

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.history = self._read_param("history", 200, int, strict_int=True, positive=True)
        self.var_threshold = self._read_param("var_threshold", 16, float, positive=True)
        self.detect_shadows = self._read_param("detect_shadows", False, bool)
        # MOG2 学习率合法区间 [0,1]（-1 为 OpenCV 自动模式，这里不允许隐式切换）
        self.learning_rate = self._read_param("learning_rate", 0.0, float,
                                              min_val=0.0, max_val=1.0)
        # 空场初始化学习率（独立配置项；检测阶段冻结用 learning_rate）。
        # 必须 >0：apply(learningRate=0) 表示永不更新背景，空场学习会静默失效
        self.bg_learn_rate = self._read_param("bg_learn_rate", BG_LEARN_RATE, float,
                                              positive=True, max_val=1.0)
        self.bg_sub = cv2.createBackgroundSubtractorMOG2(
            history=self.history, varThreshold=self.var_threshold,
            detectShadows=self.detect_shadows)

    def _reset_impl(self, frames) -> bool:
        """空场初始化背景：重建模型后，用多帧真实画面学习背景（每帧 apply 一次），完成后冻结。
        重建语义与 Median 的"清空缓冲重建"保持一致，避免旧背景（换家具/光照变化）残留。"""
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
            # 空场学习：多帧多次 apply，指数滑动使背景趋近稳定的空场画面；
            # 学习率 <1 时旧帧权重指数衰减，人员出现/消失的帧不主导背景
            self.bg_sub.apply(gray, learningRate=self.bg_learn_rate)
            learned += 1
        if learned == 0:
            self.background_ready = False
            return False
        self.background_ready = True
        return True

    def _detect_gray(self, gray) -> dict:
        fgmask = self.bg_sub.apply(gray, learningRate=self.learning_rate)
        # 若开启阴影检测，前景掩码含 127 灰阶阴影像素，统一二值化避免误计为前景
        fgmask = cv2.threshold(fgmask, SHADOW_THRESHOLD, 255, cv2.THRESH_BINARY)[1]
        fgmask = self._apply_morphology(fgmask)
        return self._verdict(fgmask)


class MedianDetector(BaseDetector):
    """滑动统计背景 + 帧差阈值。
    周期性用最近窗口帧逐像素中值重算背景：
      - 抹掉短暂就座/路过的人影（窗口内占比<50%的目标不进入中值）；
      - 捕捉人员的持续微动（打字/玩手机/转身）与坐下/离开动作；
      - 无需纯空场，适配无空场、人员动态变化的监控素材。
    参数：diff_threshold 帧差阈值；window_frames 中值窗口；refresh_frames 重算周期。"""

    name = "median"
    supports_background_reset = True  # 背景建模类：支持背景基准重建

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.diff_threshold = self._read_param("diff_threshold", 30, float, positive=True)
        self._size_warned = False  # 尺寸不匹配告警限流
        self.window_frames = self._read_param("window_frames", 90, int,
                                              strict_int=True, positive=True)
        self.refresh_frames = self._read_param("refresh_frames", 30, int,
                                               strict_int=True, positive=True)
        # freeze_bg=true：初始化（全视频中值）后冻结背景，不再随检测滚动刷新，
        # 防止"坐着不动超过窗口"的人被学进背景导致漏检（配合 sample_mode=full_span 使用）
        self.freeze_bg = self._read_param("freeze_bg", False, bool)
        self.bg_image = None
        self._gray_buf = collections.deque(maxlen=self.window_frames)
        self._frame_count = 0

        # 互补外观判据（解决"背景中值≈静止伏案者 → 差分≈0"的漏检）：
        # 肤色先验（YCbCr Chai-Ngan 经典区间）+ 高频纹理密度（Laplacian 方差），
        # 与背景差分 OR 融合，任一判据命中即判占用；不依赖背景参考，任意视角兜底。
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
        # 防御：缓冲内帧尺寸不一致时统一到首帧尺寸（分辨率中途变化场景）
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
        # maxlen deque 已自动限制窗口长度，无需手工裁剪
        self.bg_image = self._median_bg(self._gray_buf)
        self.background_ready = True
        return True

    def _refresh_background(self):
        if len(self._gray_buf) < MIN_REFRESH_SAMPLES:
            return
        self.bg_image = self._median_bg(self._gray_buf)

    def _compute_fgmask(self, gray):
        """背景差分公共段：维护缓冲/背景并返回前景掩码；背景未就绪/样本不足返回 None。"""
        self._gray_buf.append(gray)  # maxlen deque 自动淘汰最旧帧
        self._frame_count += 1
        if not self.freeze_bg and self._frame_count % self.refresh_frames == 0:
            self._refresh_background()
        bg = self.bg_image
        if bg is None or bg.shape != gray.shape:
            # 分辨率中途变化：立即重算背景（仅缓冲内帧对齐），避免 absdiff 抛尺寸异常
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
        """median 帧级判定：差分 + 肤色/纹理外观判据 OR 融合。
        背景差分失效（背景中值≈静止伏案者）时由绝对外观判据兜底。"""
        if not self.appearance_enabled or bgr is None:
            return self._detect_gray(gray)
        fgmask = self._compute_fgmask(gray)
        if fgmask is None:
            return {}
        return self._verdict_fused(fgmask, gray, bgr)

    def _verdict_fused(self, fgmask, gray, bgr) -> dict:
        """差分占比 + 肤色占比 + 纹理占比融合判占：
        strength = max(差分强度, 肤色强度, 纹理强度)，任一判据强度 ≥1 即判占用。
        差分失效时由绝对外观判据兜底，解决"静止伏案者"漏检。"""
        frame_h = fgmask.shape[0]
        frame_w = fgmask.shape[1]
        result = {}
        for seat_no, bbox in self.seats.items():
            rect = self._roi_rect(bbox, frame_w, frame_h)
            if rect is None:
                # ROI 越界：标记为不可判定，与基类 _verdict 语义一致
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

            # confidence 语义与基类 _verdict 一致：占用越强越高，空闲越远离阈值越高
            if occupied:
                confidence = min(strength / (strength + 1.0), 0.95)
            else:
                confidence = max(1.0 - strength, 0.0)
            result[seat_no] = {"occupied": occupied,
                               "confidence": round(confidence, CONFIDENCE_ROUND_DIGITS),
                               "valid": True}
        return self._debounce_apply(result)

    def _appearance_score(self, gray, bgr, rect):
        """返回 (skin_ratio, tex_ratio) 两个归一化外观占比（0..1）。
        skin_ratio：YCbCr 肤色占比（强先验，伏案者露出的面/颈/手）；
        tex_ratio：Laplacian 方差归一化纹理占比（任意视角兜底，头发/衣物/轮廓）。"""
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
        # 显示框跟随：座位框大小随"镜头中座位/人的实际大小"动态平滑调整
        # （近大远小自适应），判定 ROI 仍用静态 self.seats，显示与判定解耦
        self.display_smooth = self._read_param("display_smooth", 0.3, float,
                                               min_val=0.0, max_val=1.0)
        self.follow_person = self._read_param("display_follow_person", True, bool)
        self.follow_chair = self._read_param("display_follow_chair", True, bool)
        # 最近 chair 跟随的距离上限（工作域像素；0=自动取座位静态框对角线*1.5，
        # 防止本座位椅子被遮挡时远处椅子劫持显示框）
        self.follow_chair_max_dist = self._read_param("follow_chair_max_dist", 0.0, float, min_val=0.0)
        self.display_boxes = {}  # seat_no -> [x1,y1,x2,y2]（工作域显示框，随镜头目标大小平滑变化）
        self._net = None
        self._tmp_model = None  # 中文路径规避：复制到英文临时目录的模型路径
        self._last_infer_ts = 0.0
        self._size_warned = False

    def detect(self, frame) -> dict:
        """YOLO 覆写：推理移出主锁（独立 _infer_lock 串行化前向，结果经短锁发布），
        MJPEG 画框线程不再被数百 ms CPU 前向阻塞，监控流帧率显著提升。
        判定语义不变：占用仍按静态 seats ROI，防抖/结果缓存与基类一致。"""
        if not self.background_ready:
            return {}
        gray = self._preprocess(frame)
        if gray is None:
            return {}
        bgr = self._preprocess_bgr(frame)
        final = self._detect_frame(gray, bgr)
        return {k: dict(v) for k, v in final.items()}

    # ---------------- 显示框跟随（画框自适应镜头中座位大小） ----------------

    def get_display_boxes(self) -> dict:
        """持锁拷贝显示框（供 app.py 画框线程读取，避免与检测线程竞态）"""
        with self._lock:
            return {k: list(v) for k, v in self.display_boxes.items()}

    def _update_display_box(self, seat_no, target, work_h):
        """显示框平滑更新：新框 = 旧框 + smooth*(目标框-旧框)，抑制逐帧跳变。
        target: [x1,y1,x2,y2] 工作域坐标；无旧框时直接采用目标框。
        work_h: 工作域高度，用于最小尺寸保护扩框后 clamp 到帧边界。"""
        with self._lock:  # 与 get_display_boxes 持锁读取互斥
            old = self.display_boxes.get(seat_no)
            if old is None:
                self.display_boxes[seat_no] = [float(v) for v in target]
                return
            a = self.display_smooth
            nb = [old[i] + a * (target[i] - old[i]) for i in range(4)]
            # 最小尺寸保护：宽/高不低于 chair 最小阈值，防止检测抖动把框缩没
            if nb[2] - nb[0] < self.min_chair_w:
                c = (nb[0] + nb[2]) / 2.0
                nb[0], nb[2] = c - self.min_chair_w / 2.0, c + self.min_chair_w / 2.0
            if nb[3] - nb[1] < self.min_chair_h:
                c = (nb[1] + nb[3]) / 2.0
                nb[1], nb[3] = c - self.min_chair_h / 2.0, c + self.min_chair_h / 2.0
            # clamp 到工作域边界（靠近画面边缘时对称扩框可能越界，PIL 裁剪不崩但框位失真）
            nb[0] = max(0.0, min(float(WORK_WIDTH), nb[0]))
            nb[2] = max(0.0, min(float(WORK_WIDTH), nb[2]))
            nb[1] = max(0.0, min(float(work_h), nb[1]))
            nb[3] = max(0.0, min(float(work_h), nb[3]))
            self.display_boxes[seat_no] = nb

    def _display_target_for(self, seat_no, bbox, persons, chairs):
        """计算某座位本帧的显示框目标（工作域坐标）：
        1) 关联到坐姿 person → 目标=人体框（框随人实际大小）；
        2) 否则最近 chair 检测框 → 目标=椅子框（近大远小自适应）；
        3) 都没有 → 目标=静态座位 bbox（保持基准）。"""
        x1, y1, x2, y2 = bbox
        sx, sy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
        best_p = None
        best_score = 0.0
        for (pbox, score) in persons:
            px1, py1, pw, ph = pbox
            if ph <= 0 or pw / ph < self.sit_aspect_min:
                continue
            px2, py2 = px1 + pw, py1 + ph
            ix1, iy1 = max(x1, px1), max(y1, py1)
            ix2, iy2 = min(x2, px2), min(y2, py2)
            inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
            union = (x2 - x1) * (y2 - y1) + (px2 - px1) * (py2 - py1) - inter
            iou = inter / union if union > 0 else 0.0
            center_in = (sx >= px1 and sx <= px2 and sy >= py1 and sy <= py2)
            if (iou >= self.occupy_overlap or center_in) and score >= best_score:
                best_score = score
                best_p = (px1, py1, px2, py2)
        if best_p is not None and self.follow_person:
            return list(best_p)
        if self.follow_chair:
            # 最近 chair：框中心距离座位中心最近者；带距离上限防止远处椅子劫持显示框
            diag = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
            limit = self.follow_chair_max_dist if self.follow_chair_max_dist > 0 else diag * 1.5
            best_c = None
            best_d = None
            for (cbox, _score) in chairs:
                cx, cy, cw, ch = cbox
                if cw < self.min_chair_w or ch < self.min_chair_h:
                    continue
                ccx, ccy = cx + cw / 2.0, cy + ch / 2.0
                d = ((ccx - sx) ** 2 + (ccy - sy) ** 2) ** 0.5
                if d > limit:
                    continue  # 距离超限的椅子不参与跟随（视为被遮挡）
                if best_d is None or d < best_d:
                    best_d = d
                    best_c = (cx, cy, cx + cw, cy + ch)
            if best_c is not None:
                return list(best_c)
        return [float(v) for v in bbox]

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
            # （横屏 r=1 恰好掩盖该问题，竖屏 r<1 时 w/h 若不除会坐标域混用）
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
        """批量检测 chair → 按中心距离聚类生成稳定座位集合（自动标定）。
        重复调用会重新识别座位（等价"重置背景基准"= 重新识别座位）。"""
        # 重标定期间先置未就绪：并发 /detect 与 MJPEG 的 detect() 快速空返回，
        # 避免阻塞在推理锁上数秒（重标定需 label_frames 次前向）；成功路径末尾恢复 True
        self.background_ready = False
        with self._infer_lock:  # 模型加载与推理共用推理锁（与在线检测互斥，避免 _net 双写）
            self._ensure_net()
        cands = []  # 座位候选 (cx, cy, x, y, w, h)：chair 全量 + 坐姿 person 下半框
        for frame in (frames or [])[:self.label_frames]:
            bgr = self._preprocess_bgr(frame)
            if bgr is None:
                continue
            with self._infer_lock:
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
            # 保留 self.seats：手工预置基线不清空，避免一次失败永久丢失
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
        # 不变式：self.seats / self.display_boxes 只允许整体重绑定（换引用），
        # 禁止原地增删改（seats[k]=... / .clear()）——检测与画框线程无锁迭代依赖此不变式
        self.seats = merged
        self.display_boxes = {k: [float(v) for v in b] for k, b in merged.items()}
        self.background_ready = True
        self._debounce.clear()
        self.last_result = {}  # 重绑定而非原地 clear（与无锁读取线程安全共存）
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
        work_h = gray.shape[0] if gray is not None else 0  # 工作域高度（显示框 clamp 用）
        bgr = bgr if bgr is not None else gray
        with self._infer_lock:
            # 节流判断与置时间戳整体移入推理锁：并发调用方（MJPEG/后端轮询）
            # 只有一个能通过判断执行前向，其余直接复用最近结果（消除 TOCTOU 冗余推理）。
            # 锁内深拷贝 last_result：reset 用整体重绑定（无原地变异），旧 dict 不会被
            # 改写，无锁拷贝安全；此处不取主锁，与 reset 的 _lock→_infer_lock 锁序不构成环。
            now = time.time()
            if now - self._last_infer_ts < self.cache_interval and self.last_result:
                cached = {k: dict(v) for k, v in self.last_result.items()}
                return cached
            self._last_infer_ts = now
            res = self._infer(bgr)
        persons = res.get(self.COCO_PERSON, [])
        chairs = res.get(self.COCO_CHAIR, [])
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
                    if score >= best_score:
                        best_score = score
                        occupied = True
            # 显示框跟随：本帧目标框（人体/椅子/静态基准）→ 平滑更新
            target = self._display_target_for(seat_no, bbox, persons, chairs)
            self._update_display_box(seat_no, target, work_h)
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

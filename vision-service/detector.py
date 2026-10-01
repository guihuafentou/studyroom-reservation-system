# -*- coding: utf-8 -*-
"""
座位占用检测器
核心设计：MOG2 背景建模 + 冻结背景（learningRate=0）。
背景基准在"空场"时初始化（POST /reset 或启动自动初始化），之后不再更新，
因此任何与空背景存在差异的目标（静坐的人、占座的书包水杯）都会持续显示为前景，
匹配"治理占座"目标。检测器接口化，可扩展 YOLOv8（见 yolo_detector.py 预留位）。

检测流程：
  帧 → 缩放 → 灰度 → MOG2(learningRate=0) 前景掩码
     → 形态学开闭运算 → 轮廓过滤(面积/宽高比)
     → 与座位 ROI(bbox) 重叠面积占比判定 → 连续 debounce_frames 帧防抖
"""
import json
import os
import time

import cv2
import numpy as np

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

WORK_WIDTH = 640  # 统一缩放宽度，ROI 坐标基于该尺寸标注


class BaseDetector:
    """检测器抽象接口：后续 YOLOv8 实现同一接口即可无缝替换"""

    name = "base"

    def reset_background(self, frame) -> bool:
        raise NotImplementedError

    def detect(self, frame) -> dict:
        """返回 {seat_no: {'occupied': bool, 'confidence': float}}"""
        raise NotImplementedError


class Mog2Detector(BaseDetector):
    name = "mog2"

    def __init__(self, cfg: dict):
        m = cfg.get("mog2", {})
        self.history = int(m.get("history", 200))
        self.var_threshold = float(m.get("var_threshold", 16))
        self.detect_shadows = bool(m.get("detect_shadows", False))
        self.learning_rate = float(m.get("learning_rate", 0.0))
        self.min_area = float(m.get("min_area", 800))
        self.min_w = float(m.get("min_w", 30))
        self.min_h = float(m.get("min_h", 30))
        self.min_ratio = float(m.get("min_ratio", 0.25))
        self.max_ratio = float(m.get("max_ratio", 4.0))
        self.overlap_threshold = float(m.get("overlap_threshold", 0.5))
        self.debounce_frames = int(m.get("debounce_frames", 3))
        self.reset_frames = int(m.get("reset_frames", 30))

        # 座位 ROI：seat_no -> [x1, y1, x2, y2]
        self.seats = {}
        for s in cfg.get("seats", []):
            bbox = s.get("bbox")
            if bbox and len(bbox) == 4:
                self.seats[s["seat_no"]] = [int(v) for v in bbox]

        self.bg_sub = cv2.createBackgroundSubtractorMOG2(
            history=self.history, varThreshold=self.var_threshold,
            detectShadows=self.detect_shadows)
        self.background_ready = False
        # 防抖状态: seat_no -> [连续帧的占用判定列表]
        self._history = {}
        # 最近检测结果
        self.last_result = {}

    # ---------------- 工具 ----------------

    @staticmethod
    def _preprocess(frame) -> tuple:
        """缩放 + 灰度，返回 (工作帧, 原尺寸比例)"""
        h, w = frame.shape[:2]
        scale = WORK_WIDTH / w
        work_w = WORK_WIDTH
        work_h = int(h * scale)
        resized = cv2.resize(frame, (work_w, work_h))
        gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)
        return gray, scale

    @staticmethod
    def _roi_rect(bbox):
        x1, y1, x2, y2 = bbox
        return max(x1, 0), max(y1, 0), min(x2, 639), min(y2, 639)

    @staticmethod
    def _overlap_ratio(contour_bbox, roi):
        """轮廓外接框与 ROI 的重叠面积占比（相对 ROI 面积）"""
        cx1, cy1, cx2, cy2 = contour_bbox
        rx1, ry1, rx2, ry2 = roi
        ox1, oy1 = max(cx1, rx1), max(cy1, ry1)
        ox2, oy2 = min(cx2, rx2), min(cy2, ry2)
        if ox2 <= ox1 or oy2 <= oy1:
            return 0.0
        inter = (ox2 - ox1) * (oy2 - oy1)
        roi_area = max((rx2 - rx1) * (ry2 - ry1), 1)
        return inter / roi_area

    # ---------------- 背景管理 ----------------

    def reset_background(self, frames) -> bool:
        """空场初始化背景：用多帧真实画面学习背景模型（每帧 apply 一次），完成后冻结"""
        if not frames:
            return False
        for frame in frames:
            gray, _ = self._preprocess(frame)
            # 学习率 0.01 缓慢收敛背景
            self.bg_sub.apply(gray, learningRate=0.01)
        self.background_ready = True
        return True

    # ---------------- 检测 ----------------

    def detect(self, frame) -> dict:
        if not self.background_ready:
            return self.last_result

        gray, _ = self._preprocess(frame)
        fgmask = self.bg_sub.apply(gray, learningRate=self.learning_rate)

        # 形态学：开运算去噪点，闭运算填充空洞
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        fgmask = cv2.morphologyEx(fgmask, cv2.MORPH_OPEN, kernel)
        fgmask = cv2.morphologyEx(fgmask, cv2.MORPH_CLOSE, kernel)

        # 轮廓检测
        contours, _ = cv2.findContours(fgmask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        boxes = []
        for c in contours:
            area = cv2.contourArea(c)
            if area < self.min_area:
                continue
            x, y, w, h = cv2.boundingRect(c)
            if w < self.min_w or h < self.min_h:
                continue
            ratio = h / max(w, 1e-6)
            if not (self.min_ratio <= ratio <= self.max_ratio):
                continue
            boxes.append((x, y, x + w, y + h, area))

        # 座位 ROI 判定
        result = {}
        for seat_no, bbox in self.seats.items():
            roi = self._roi_rect(bbox)
            best = 0.0
            for (x1, y1, x2, y2, _area) in boxes:
                best = max(best, self._overlap_ratio((x1, y1, x2, y2), roi))
            occupied = best >= self.overlap_threshold
            confidence = min(best / max(self.overlap_threshold, 1e-6), 1.0) if occupied else max(1.0 - best / self.overlap_threshold, 0.0)
            result[seat_no] = {"occupied": occupied, "confidence": round(confidence, 2)}

        # 防抖：最近 debounce_frames 帧多数表决，抑制抖动误判
        final = {}
        for seat_no, r in result.items():
            hist = self._history.setdefault(seat_no, [])
            hist.append(r["occupied"])
            if len(hist) > self.debounce_frames:
                hist.pop(0)
            votes = sum(1 for x in hist if x)
            majority = votes >= (len(hist) + 1) // 2
            final[seat_no] = {"occupied": majority, "confidence": r["confidence"]}

        self.last_result = final
        return final


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def create_detector(cfg: dict) -> BaseDetector:
    name = cfg.get("detector", "mog2")
    if name == "mog2":
        return Mog2Detector(cfg)
    # 预留 YOLOv8 扩展位：yolo_detector.py 实现 BaseDetector 后在此接入
    raise ValueError("不支持的检测器: %s（可用: mog2）" % name)


if __name__ == "__main__":
    # 自测：读取视频源若干帧进行背景初始化与检测
    cfg = load_config()
    det = create_detector(cfg)
    src = cfg["video_source"]
    cap = cv2.VideoCapture(src)
    ok, frame = cap.read()
    if not ok:
        print("无法读取视频源:", src)
    else:
        frames = [frame]
        for _ in range(det.reset_frames - 1):
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

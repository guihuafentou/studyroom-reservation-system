# -*- coding: utf-8 -*-
"""
校园自习室预约管理系统 - 视觉识别服务（FastAPI + OpenCV）
统一集成模式：后端定时轮询本服务的 GET /detect 获取座位占用结果。

接口：
    GET  /health           健康检查（含检测器与背景就绪状态）
    GET  /detect?room_id=  抽帧检测，返回各座位占用状态
    POST /reset            重建背景基准（请在自习室空场时调用）

启动：
    uvicorn app:app --host 127.0.0.1 --port 8001
（默认仅监听本机，内部接口不对外暴露）
"""
import logging
import threading
import time
from typing import Optional

import cv2
from fastapi import FastAPI, Query

from detector import create_detector, load_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("vision")

app = FastAPI(title="自习室座位占用检测服务", version="1.0.0")

# 全局配置与检测器（单例）
CFG = load_config()
DETECTOR = create_detector(CFG)

# 视频捕获：video_source 为 int=摄像头索引, str=rtsp/文件路径
_VIDEO_SOURCE = CFG.get("video_source", 0)
_capture = None
_capture_lock = threading.Lock()
_last_frame_time = 0.0
_frame_interval = float(CFG.get("frame_interval", 1.0))


def _get_capture():
    global _capture
    if _capture is None or not _capture.isOpened():
        _capture = cv2.VideoCapture(_VIDEO_SOURCE)
        if not _capture.isOpened():
            logger.error("无法打开视频源: %s", _VIDEO_SOURCE)
            _capture = None
            raise RuntimeError("视频源不可用: %s" % _VIDEO_SOURCE)
        # 首次打开后自动尝试初始化背景（若配置 auto_init）
    return _capture


def _read_frame():
    """读取一帧（文件型视频播放结束自动回绕）"""
    global _last_frame_time, _capture
    with _capture_lock:
        cap = _get_capture()
        ok, frame = cap.read()
        if not ok:
            # 文件型视频源播放结束：回绕重播
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = cap.read()
        if ok:
            _last_frame_time = time.time()
        return frame


def _read_frames(n):
    """连续读取 n 帧（不足则重复最后一帧补齐）"""
    frames = []
    last = None
    for _ in range(n):
        f = _read_frame()
        if f is None:
            break
        frames.append(f)
        last = f
    while len(frames) < n and last is not None:
        frames.append(last)
    return frames


@app.get("/health")
def health():
    return {
        "status": "ok",
        "detector": DETECTOR.name,
        "background_ready": DETECTOR.background_ready,
        "video_source": str(_VIDEO_SOURCE),
        "seats": list(DETECTOR.seats.keys()),
        "room_id": CFG.get("room_id"),
    }


@app.get("/detect")
def detect(room_id: Optional[int] = Query(default=None, description="自习室ID（预留多教室扩展）")):
    try:
        frame = _read_frame()
    except Exception as e:
        return {"error": str(e)}

    if frame is None:
        return {"error": "无法获取视频帧", "results": []}

    if not DETECTOR.background_ready:
        # 背景未初始化：用多帧自动初始化（请确认画面为空场）
        frames = _read_frames(DETECTOR.reset_frames)
        DETECTOR.reset_background(frames)
        logger.info("检测到背景未初始化，已用当前画面自动初始化（请确认画面为空场）")
        return {"warning": "背景已自动初始化（当前画面被作为基准）", "background_ready": True, "results": []}

    result = DETECTOR.detect(frame)
    results = [{"seat_no": k, "occupied": v["occupied"], "confidence": v["confidence"]}
               for k, v in result.items()]
    return {"room_id": room_id or CFG.get("room_id"), "detector": DETECTOR.name, "results": results}


@app.post("/reset")
def reset():
    """重建背景基准：请在自习室空场时调用（先清空座位区域）"""
    try:
        frames = _read_frames(DETECTOR.reset_frames)
        if not frames:
            return {"error": "无法获取视频帧"}
        DETECTOR.reset_background(frames)
        # 清空防抖历史
        DETECTOR._history.clear()
        return {"status": "ok", "message": "背景基准已重建", "background_ready": True}
    except Exception as e:
        logger.error("背景重置失败: %s", e)
        return {"error": str(e)}


@app.on_event("shutdown")
def shutdown():
    global _capture
    if _capture is not None:
        _capture.release()

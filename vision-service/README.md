# 视觉识别服务

基于 OpenCV MOG2 背景建模（**冻结背景**策略）的座位占用检测服务，负责"视觉识别"模块中的**座位占用检测**（非人脸识别），为预约系统的占座治理提供实时占用数据。

## 技术要点

| 项目 | 说明 |
|---|---|
| 检测任务 | 座位是否被占用（静坐的人 / 占座物品均视为"占用"） |
| 算法 | MOG2 背景建模（Zivkovic, 2004），`learningRate=0` 冻结背景 |
| 背景基准 | 空场时初始化（启动后首次检测自动初始化 / `POST /reset` 手动重建） |
| 判定方式 | 前景轮廓与座位 ROI（bbox）重叠面积占比 ≥ 阈值 → 占用 |
| 防抖 | 连续 N 帧多数表决，抑制闪烁误判 |
| 服务形式 | FastAPI，仅监听 `127.0.0.1:8001`，供后端轮询 |

## 目录结构

```
vision-service/
├── app.py          # FastAPI 服务（/health /detect /reset）
├── detector.py     # MOG2 冻结背景检测器（BaseDetector 接口，预留 YOLOv8 扩展位）
├── config.json     # 视频源、检测参数、座位 ROI 标定
└── requirements.txt
```

## 安装与启动

> 建议使用 Python 3.10 ~ 3.12 环境（opencv-python 对 3.13+ 可能缺少预编译轮子）。

```bash
# 1. 创建环境（示例，conda）
conda create -n studyroom-vision python=3.11 -y
conda activate studyroom-vision

# 2. 安装依赖
cd vision-service
pip install -r requirements.txt

# 3. 启动（仅监听本机）
uvicorn app:app --host 127.0.0.1 --port 8001
```

## 配置说明（config.json）

```jsonc
{
  "video_source": 0,          // 视频源：0=USB摄像头索引；"rtsp://..."=网络摄像头；"demo.mp4"=本地视频文件
  "room_id": 1,               // 默认监控的自习室ID
  "frame_interval": 1.0,      // 抽帧间隔（秒）
  "detector": "mog2",         // 检测器类型（预留 yolo）
  "mog2": {
    "learning_rate": 0.0,     // 冻结背景：0=不更新背景模型
    "overlap_threshold": 0.5, // 前景与座位ROI重叠占比阈值（0~1）
    "min_area": 800,          // 前景轮廓最小面积（过滤噪点）
    "debounce_frames": 3      // 防抖帧数（多数表决）
  },
  "seats": [                  // 座位 ROI 标定（坐标基于 640 宽画面，[x1,y1,x2,y2]）
    { "seat_no": "A-01", "bbox": [60, 60, 130, 130] },
    ...
  ]
}
```

### 如何标定座位 ROI

1. 运行 `python detector.py`（自测脚本）或启动服务后打开 `/health`；
2. 用任意截图工具获取 640 宽度的自习室画面；
3. 为每个座位框选矩形区域，将 `[x1, y1, x2, y2]` 填入 `config.json` 的 `seats`；
4. `seat_no` 必须与数据库 `seat.seat_no` 一致（见 `database/init.sql`）。

### 摄像头不可用时的演示方式

无摄像头 / 无 GPU 环境可直接使用**本地视频文件**模拟：把一段自习室监控录像放到本目录，将 `video_source` 改为文件名（如 `"demo.mp4"`），即可让整条"检测 → 后端轮询 → 前端展示"链路跑通。

## 接口一览

| 方法 | 路径 | 说明 | 返回要点 |
|---|---|---|---|
| GET | `/health` | 健康检查 | 检测器、背景就绪、座位列表 |
| GET | `/detect?room_id=1` | 抽帧检测 | `results: [{seat_no, occupied, confidence}]` |
| POST | `/reset` | 重建背景基准（需空场） | 提示信息 |

## 隐私说明

本服务**只计算占用状态与置信度**，不保存、不传输视频画面本身。检测日志（占用/置信度/时间）由后端写入 `seat_status_log` 表。

## 与后端集成

后端 `VisionSyncTask` 每 5 秒轮询一次 `GET /detect`，将结果与预约数据合并后：
- 空闲 + 无预约 → `seat.status = 1 空闲`
- 占用 + 有有效预约 → `seat.status = 3 使用中`
- 占用 + 无预约（疑似占座）→ 记录检测日志并在管理端"视觉监控"页呈现，供管理员处置

后端连接配置：`backend/src/main/resources/application.yml` 的 `studyroom.vision.base-url`。

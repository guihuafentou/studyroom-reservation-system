# 基于 SpringBoot + Vue 与视觉识别的校园自习室预约管理系统

一个面向高校图书馆/自习室的座位预约管理系统：学生按**离散时段**预约座位、扫码/一键签到、违约治理；后端集成 **OpenCV 视觉识别服务**（MOG2 冻结背景的座位占用检测）治理"占座"问题；管理员拥有完整的自习室、座位、时段、预约、用户与审计管理能力。

---

## 一、功能总览

| 角色 | 功能 |
|---|---|
| 学生 | 注册/登录、浏览自习室、按日期+时段查看座位图、预约/取消/签到、查看我的预约与违约记录 |
| 管理员 | 数据看板（趋势/时段热度/上座率）、用户启停、自习室/座位/时段维护、预约管理与强制取消、视觉监控、操作审计 |
| 视觉服务 | 实时座位占用检测（冻结背景 MOG2）、背景重置、检测日志落库 |

核心机制（v2.1 设计要点）：

- **离散时段模型**：每天 08:00-22:00 按 30 分钟切分为时段（`slot` 表），预约精确到"座位 × 日期 × 时段"。
- **防重叠双唯一索引**：`reservation` 表通过生成列条件唯一索引（仅"进行中"状态占坑）同时保证**同座位同时段不重复**、**同用户同时段不超 1 座**；取消/违约/完成后自动释放，可重新预约。
- **占座治理**：视觉服务用 MOG2 **冻结背景**（`learningRate=0`，空场建基线），静坐与占座物品都持续显示为前景；"空闲+无预约但被占用"即疑似占座，展示在管理端视觉监控页。
- **违约闭环**：超时未签到判违约；违约满 3 次禁约 7 天；取消截止时段开始前 30 分钟。
- **审计**：管理员强制取消、启停用户、改座位等关键操作全部写入 `admin_audit_log`。

## 二、技术栈

| 层 | 技术 |
|---|---|
| 前端 | Vue 3 + Vite + Element Plus + Pinia + Vue Router + Axios + ECharts |
| 后端 | Spring Boot 3.3.5 + MyBatis-Plus 3.5.7 + JWT(jjwt 0.12.6) + Spring Security Crypto(BCrypt) |
| 数据库 | MySQL 8.0+（utf8mb4） |
| 视觉 | Python 3.10-3.12 + FastAPI + OpenCV（MOG2） |

## 三、目录结构

```
自习室预约管理系统/
├── docs/
│   └── 设计方案.md            # 系统设计方案 v2.1（含需求、架构、库表、接口、文献）
├── database/
│   └── init.sql               # 建库建表 + 初始化数据（2 自习室 / 50 座位 / 每室 28 时段）
├── backend/                   # Spring Boot 后端
│   ├── pom.xml
│   └── src/main/
│       ├── java/com/campus/studyroom/   # 46 个类：common/security/config/entity/mapper/dto/vo/service/controller/task
│       └── resources/application.yml
├── frontend/                  # Vue 3 前端
│   └── src/{api,store,router,views}
├── vision-service/            # Python 视觉识别服务
│   ├── app.py                 # FastAPI: /health /detect /reset
│   ├── detector.py            # MOG2 冻结背景检测器
│   ├── config.json            # 视频源 / 检测参数 / 座位 ROI
│   └── README.md
└── ai-bridge/                 # 本机 Claude(DeepSeek) 辅助评审记录
    ├── design_review.txt / design_recheck.txt   # 方案两轮评审
    └── claude_reviewer.py     # 代码评审调用脚本（备用）
```

## 四、环境要求

| 软件 | 版本 |
|---|---|
| JDK | 17+（本项目在 JDK 23 编译运行通过） |
| Maven | 3.6+ |
| Node.js | 18+（实测 22） |
| MySQL | 8.0+ |
| Python | 3.10 ~ 3.12（视觉服务） |

## 五、快速启动

### 0. 初始化数据库

```bash
mysql -uroot -p < database/init.sql
```

> 数据库连接在 `backend/src/main/resources/application.yml` 配置，**密码无默认值**，启动前必须通过环境变量 `MYSQL_PASSWORD` 注入（见下）。

### 1. 启动后端

```bash
cd backend
# 先注入三个环境变量（值按你的实际环境填写，不入代码）
export MYSQL_PASSWORD=你的MySQL密码
export JWT_SECRET=一段随机长字符串（生产环境必改）
export STUDYROOM_ADMIN_PASSWORD=初始管理员密码
mvn spring-boot:run
```

> Windows PowerShell 下用 `$env:MYSQL_PASSWORD="你的MySQL密码"` 形式设置；三个变量**没有默认值**，缺失会导致启动失败（这是刻意的：凭据只经环境变量注入，代码中零密钥）。

- 首次启动自动创建管理员账号：**admin**（密码取 `STUDYROOM_ADMIN_PASSWORD` 环境变量）
- 服务监听 `http://localhost:8080`

### 2. 启动视觉识别服务（可选，座位占用检测用）

```bash
cd vision-service
pip install -r requirements.txt
uvicorn app:app --host 127.0.0.1 --port 8001
```

- 无摄像头时：把一段自习室视频放到本目录，`config.json` 的 `video_source` 改成文件名即可模拟，链路照常跑通。
- 座位 ROI 标定见 `vision-service/README.md`。

### 3. 启动前端

```bash
cd frontend
npm install
npm run dev
```

- 访问 `http://localhost:5173`
- 学生账号：注册页自建；管理员：admin（密码为启动时注入的 `STUDYROOM_ADMIN_PASSWORD`）

## 六、默认账号与配置

| 项 | 值 | 修改位置 |
|---|---|---|
| 管理员 | admin / 由环境变量注入（无默认值） | 启动前设置 `STUDYROOM_ADMIN_PASSWORD` |
| MySQL | root / 由环境变量注入（无默认值） | 启动前设置 `MYSQL_PASSWORD` |
| JWT 密钥 | 由环境变量注入（无默认值） | 启动前设置 `JWT_SECRET`（生产必改） |
| 视觉服务 | http://127.0.0.1:8001 | `application.yml` → `studyroom.vision.base-url` |
| 视觉轮询 | 5 秒 | `application.yml` → `studyroom.vision.poll-interval-ms` |

## 七、主要接口

| 模块 | 接口 | 说明 |
|---|---|---|
| 认证 | POST `/api/auth/register` `/login` GET `/api/auth/me` | 注册 / 登录(JWT) / 当前用户 |
| 自习室 | GET `/api/rooms` `/api/rooms/{id}` `/api/rooms/{id}/slots` `/api/rooms/{id}/seats?date=&slotId=` | 列表 / 详情 / 时段 / 座位状态 |
| 预约 | POST `/api/reservations` GET `/api/reservations/mine` PUT `/api/reservations/{id}/cancel` `/sign` | 预约 / 我的 / 取消 / 签到 |
| 视觉 | GET `/api/vision/status?roomId=` GET `/api/vision/service/status` POST `/api/vision/reset` | 座位状态 / 服务健康 / 背景重置 |
| 管理 | `/api/admin/**` | 用户、自习室、座位、时段、预约、审计、统计 |

完整接口定义与请求/响应示例见 `docs/设计方案.md` §6。

## 八、参考文献

设计文档引用的核心文献（均已联网核实，真实可查）：

1. Z. Zivkovic, "Improved adaptive Gaussian mixture model for background subtraction," ICPR 2004, IEEE, pp. 28-31, DOI: 10.1109/ICPR.2004.1333992 —— MOG2 背景建模算法出处
2. J. Redmon et al., "You Only Look Once: Unified, Real-Time Object Detection," CVPR 2016, pp. 779-788, arXiv:1506.02640 —— 视觉检测可扩展方向
3. M. Jones, J. Bradley, N. Sakimura, "JSON Web Token (JWT)," RFC 7519, IETF, 2015, DOI: 10.17487/RFC7519 —— 登录令牌标准
4. 郭慧敏 等, 《基于SpringBoot+微信小程序的自习室座位预约系统》, 《电脑编程技巧与维护》2026(5):38-40,65 —— 同类系统设计参考
5. 《手机端自习室预约系统的设计与实现》, 万方, 2024 —— 同类系统设计参考
6. 霍春阳, 《Vue.js设计与实现》, 人民邮电出版社, 2022, ISBN 978-7-115-58386-4 —— 前端响应式原理
7. Spring Boot / Vue.js / MyBatis-Plus / OpenCV 官方在线文档

## 九、备注

- 本项目由"豆包(Doubao) 主控 + 本机 Claude(接入 DeepSeek 模型) 辅助评审"协作完成：方案经两轮 Claude 评审（4 个致命问题 + 3 个隐患均已修复）；代码经 Claude 全量审查（`ai-bridge/code_review.txt`，2 致命 + 10 重要 + 10 建议）并**逐项修复回归**：管理员类级鉴权越权、视觉 /detect 响应结构不匹配、已完成状态机流转、非视觉房间违约兜底、时段重叠校验、定时任务条件更新防竞态、RestTemplate 超时、CORS 预检放行与来源收紧、去明文密码日志、密钥环境变量注入、业务异常真实 HTTP 状态码、前端多时段连续预约与本地日期，追求"第一次就写完善"。
- 视觉识别为**座位占用检测**（非人脸识别），不保存画面，隐私合规。
- 违约口径说明（答辩可引述）：**视觉覆盖的房间**（默认自习室1）未签到超时由视觉确认空座后判违约、占用则等待；**视觉未覆盖的房间/座位**（自习室2或未标定 ROI 的座位）无视觉证据，超时未签到按规则直接判违约（可保证规则一致性）；**视觉服务故障**时超时未签到仅释放、不计违约（技术故障不惩罚学生）。
- 生产部署建议：关闭 Swagger（未引入）、更换 JWT 密钥（环境变量 `JWT_SECRET`）、管理员初始密码用环境变量 `STUDYROOM_ADMIN_PASSWORD` 覆盖、视觉服务保持仅本机监听、数据库独立账号。

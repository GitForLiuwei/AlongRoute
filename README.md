# 顺路下车 · AlongRoute

一个面向出发前规划的 Web/H5 应用：司机从 A 前往 B，乘客在中途 P 下车，再打车、步行或乘公共交通前往 C。系统结合司机增加时间、乘客全程耗时和后续费用推荐下车候选。

**当前实现：枢纽优先召回 → 普通道路补充 → 可选 OSRM 粗排 → 高德路线核验 → 打车 / 公交分别排名。** 用户页只显示高德底图，通过 SSE 实时展示选点过程，计算完成立即显示最终方案。

> 真实候选均为 `unverified`，需与司机确认临停条件。地图可达性不等于允许停车；不得在高速主线、匝道等禁停路段上下客。

## 文档导航

| 想了解的内容 | 文档 |
|---|---|
| 安装运行、配置和常用命令 | 本页 |
| 召回、剪枝、路线核验与评分 | [选点与推荐算法](docs/PLANNING.md) |
| 请求参数、响应字段和 SSE | [API 说明](docs/API.md) |
| 实时地图、日志和候选回放 | [实时进度与回放](docs/SEARCH_REPLAY.md) |
| Linux 部署、systemd 和 OSRM | [部署与运维](docs/DEPLOY_LINUX.md) |
| 高德控制台和密钥配置 | [高德 Key 配置](docs/AMAP_KEYS_ONLY_YOU.md) |
| 目录与代码阅读顺序 | [项目结构](docs/ARCHITECTURE.md) |
| 修改与验证历史 | [算法改造记录](docs/algorithm-upgrade.md)、[项目上下文](PROJECT_CONTEXT.md) |

本文与算法说明按 2026-11-12 的当前代码整理；历史文档中的旧参数以当前实现为准。

## 主要功能

- 地点搜索：选择共同出发地 A、司机终点 B、乘客终点 C。
- 约束与偏好：司机增加时间、打车预算、全程步行和换乘上限，以及均衡 / 省时间 / 省费用三档偏好。
- 多方式推荐：同一下车点的打车和公交分别成卡，合计最多六张；满足条件时可推荐直接步行。
- 高德地图：页面打开即加载底图，实时显示候选点和核验路线；计算结束立即锁定最终结果，保留手动重播与加载重试。
- 行程详情：展示司机原路线 / 经停路线、公交乘坐步骤、到站步行、预计到达及导航分享。
- 手动测试与诊断：指定 P 测算、搜索编号、接口记录和候选事件回放。
- 演示模式：不配置 Web 服务 Key 时，API 使用明确标注的模拟地点与方案。

| 页面 | 用途 |
|---|---|
| `/`、`/user` | 用户规划页，填写行程、比较方案、查看地图和导航 |
| `/debug` | 开发调试页，查看请求、警告、评分、搜索记录和手动测算 |
| `/docs` | 后端 Swagger / OpenAPI；开发时访问 8000 端口 |

顶部切换用户页和调试页会保留当前输入和结果；刷新页面会重置会话状态。调试页不构成访问权限隔离。

## 快速开始

需要 Python 3.11+、Node.js 和 npm。建议使用 Node.js 22+；Linux 部署脚本会检查版本并按需安装项目内的 Node.js。

```bash
git clone https://github.com/GitForLiuwei/AlongRoute.git
cd AlongRoute
```

### Windows

在项目根目录打开 PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File .\setup-windows.ps1
```

然后分别在两个终端启动：

```powershell
# 终端 1
powershell -ExecutionPolicy Bypass -File .\start-backend.ps1
```

```powershell
# 终端 2
powershell -ExecutionPolicy Bypass -File .\start-frontend.ps1
```

安装脚本创建 `backend/.venv`、安装依赖，并在缺失时复制空白配置模板，不需要退出 Conda 环境。

### Linux / macOS 本地开发

在项目根目录安装依赖并创建配置文件；已有配置时保留原文件：

```bash
python3 -m venv backend/.venv
backend/.venv/bin/python -m pip install -r backend/requirements.txt
npm --prefix frontend ci
cp -n backend/.env.example backend/.env
cp -n frontend/.env.example frontend/.env.local
```

分别在两个终端启动：

```bash
# 终端 1，在项目根目录运行
PYTHONPATH=backend backend/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

```bash
# 终端 2，在项目根目录运行
npm --prefix frontend run dev
```

打开 [开发页面](http://127.0.0.1:5173/user) 和 [后端 API 文档](http://127.0.0.1:8000/docs)。Vite 将 `/api` 和 `/_AMapService` 代理至后端。

仅查看独立模拟界面时，可直接打开根目录的 `preview.html`；它不调用真实地图，也不代表当前 Vue 页面的全部功能。

## 高德与可选服务配置

| 文件 | 变量 | 用途 |
|---|---|---|
| `backend/.env` | `AMAP_WEB_SERVICE_KEY` | 真实地点、驾车、步行和公交查询；留空则使用演示 API |
| `backend/.env` | `AMAP_JS_SECURITY_CODE` | 高德 JS 安全密钥，只保存在后端 |
| `frontend/.env.local` | `VITE_AMAP_JS_KEY` | 浏览器公开的 JS API Key，用于高德底图 |
| `backend/.env` | `CANDIDATE_STRATEGY` | 默认 `hub_first`，`legacy` 用于旧流程对照 |
| `backend/.env` | `OSRM_BASE_URL`、`OSRM_DATA_VERSION` | 可选路网粗排服务地址与诊断版本；未配置时使用几何粗排 |
| `backend/.env` | `AMAP_REQUEST_INTERVAL_S` | 同接口请求启动间隔，默认 0.12 秒 |

地图底图与真实规划分别需要对应配置：演示 API 可在无 Web 服务 Key 时使用，**高德底图仍需 JS Key 和安全代理**，缺少配置时显示提示，不回退为经纬度示意图。高德控制台需允许实际访问来源，详见 [配置步骤](docs/AMAP_KEYS_ONLY_YOU.md)。

修改后端配置后重启后端；修改前端 JS Key 后重启开发服务器，生产环境需重建前端。真实密钥、`.env`、日志和缓存不提交到 GitHub，仓库仅保留空白 `.env.example`。

## 常用参数

| 参数 | 默认值 / 范围 | 含义 |
|---|---|---|
| `max_detour_minutes` | 120 / 0–120 分钟 | 司机相对 A→B 增加的时间，包含停靠 |
| `max_taxi_fare_yuan` | 200 / 0–200 元 | 下车后打车估价上限，不约束公交票价 |
| `modes` | `taxi`、`transit` | 至少选择一种后续交通方式 |
| `priority` | `balanced` | 另支持 `time`、`cost`，分别影响两种方式的排序 |
| `max_walking_m` | 3000 / 0–5000 米 | 公交全程步行上限 |
| `max_transfers` | 4 / 0–8 次 | 公交换乘上限 |
| 到站步行 | 优先 300 米，最多 480 米 | 超出 300 米部分增加评分惩罚，不修改实际到达时刻 |

乘客全程包含共同乘车、停靠和下车后行程。打车另含固定五分钟接车缓冲假设；司机总时间为 `driver_route.duration_s + stop_duration_s`。评分公式与例子见 [算法说明](docs/PLANNING.md)。

## API 概览

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/health` | 服务健康 |
| GET | `/api/config` | 规划模式与 JS 安全代理配置状态 |
| GET | `/api/places/suggest` | 地点建议 |
| POST | `/api/recommend` | 自动推荐 |
| POST | `/api/evaluate-dropoff` | 指定 P 的手动测算 |
| GET | `/api/search-replay/{search_id}` | 本次 / 历史自动规划的 JSON 回放 |
| GET | `/api/search-replay/{search_id}/stream` | 当前规划的 SSE 事件流 |

前端为每次自动推荐生成 `X-Search-ID`，在 POST 完成前订阅同一编号的事件；结果返回后不再延迟回放。实时通道使用进程内注册表，**当前应以单 worker 部署**；多 worker 需先引入共享事件通道。详细事件、字段与错误码见 [API](docs/API.md) 和 [回放说明](docs/SEARCH_REPLAY.md)。

## Linux 服务器部署

在支持 systemd 的服务器上，从项目根目录执行：

```bash
sudo bash scripts/deploy-linux.sh
```

脚本构建独立版本并注册 `alongroute.service`，默认同源监听 `0.0.0.0:8080`。防火墙和云安全组需按访问范围自行开放；部署脚本不自动调整它们。构建、健康检查和旧版本保留策略见 [部署文档](docs/DEPLOY_LINUX.md)。

```bash
systemctl status alongroute.service --no-pager
journalctl -u alongroute.service -f
systemctl restart alongroute.service
```

## 验证与已知限制

Linux 后端测试与前端检查：

```bash
backend/.venv/bin/python -m pip install -r backend/requirements-dev.txt
PYTHONPATH=backend backend/.venv/bin/python -m pytest -q backend/tests
npm --prefix frontend run build
npm --prefix frontend run test:projection
```

Windows 可在 `backend` 目录安装 `requirements-dev.txt`，设置 `$env:PYTHONPATH='.'`，再用 `.venv\Scripts\python.exe -m pytest -q` 执行后端测试。

2026-11-12 验证记录：后端 **328 / 348 项通过**，20 项枢纽早停 / 出口补充测试失败已在本轮修改前的隔离基线复现；日志、SSE、API 和生产入口专项 35 项通过。前端类型检查和构建通过；原有投影辅助测试 5 项通过、2 项失败。高德 SDK 实际加载、桌面 / 手机页面、SSE 实时候选、手动重播和失败重试均有浏览器验证。浏览器规划过程使用离线样本，不能据此证明真实行程推荐质量。

- 候选搜索有数量、时间和粗估剪枝限制，`search_complete=false` 不等于请求失败，也不保证找到全局最优点。
- 部分淘汰事件来自粗估或早停，诊断路线可能使用端点连线；不要将它们当作实际导航或完整路网结论。
- 停靠条件、道路侧、禁停标志和入口开放仍需现场确认；尚未接入实时车辆定位、网约车订单或公交实时班次。
- 请求日志和导出诊断包含行程地点与坐标，搜索编号应仅向必要人员分享。

## 技术栈与目录

前端使用 Vue 3、TypeScript、Vite、高德 JS API；后端使用 FastAPI、Pydantic、httpx；SQLite 保存地图响应和枢纽池，JSONL 保存搜索日志。

```text
backend/app/       API、地图适配、候选发现、核验与排名
backend/tests/     后端离线回归测试
frontend/src/      页面、地图、行程详情和请求封装
frontend/tests/    几何投影与候选数据辅助测试
docs/              API、算法、部署与回放文档
scripts/           Linux 部署及诊断
.runtime/          本地版本、缓存和日志，不提交 Git
```

代码入口和阅读顺序见 [项目结构](docs/ARCHITECTURE.md)。第三方服务和数据受各自条款限制；官方资料见 [高德路径规划](https://developer.amap.com/api/webservice/guide/api/newroute)、[POI 搜索](https://developer.amap.com/api/webservice/guide/api-advanced/newpoisearch) 和 [JS API 安全配置](https://developer.amap.com/api/javascript-api-v2/guide/abc/jscode)。

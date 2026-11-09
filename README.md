# 顺路下车 · AlongRoute

一个用于“司机尽量少绕路、乘客中途下车后选择打车或公共交通”的 **Windows 优先 Web/H5 MVP**。

> 当前项目包含演示模式和高德路线数据接入，不是生产级安全上下客点数据库。**所有真实 POI 都标记为待核实，不能据此确定可停车。严禁在高速主线、匝道等禁停路段上下车。**

## 已包含的功能

- Vue 3 + TypeScript + Vite 响应式网页（桌面 / 手机）
- A 共同出发地、B 司机终点、C 乘客终点的地点搜索和选择
- 司机最多增加 0–30 分钟、交通方式打车/公交、时间/费用/均衡偏好
- FastAPI 后端 API 和 Swagger 文档
- 无 Key 即可浏览的 **虚构演示模式**（全部价格和时间均为模拟数据）
- 高德 Web 服务 Key 接入后：地点 POI 搜索（含公交所需城市编码）、沿途道路及停车场候选、公交/地铁站周边候选、司机连续途经路线验证、打车与公交方案；打车比较司机增加时间与车费，公交按乘客全程耗时和偏好推荐，返回最多 6 个候选
- 公共交通下车点到实际乘车站台或地铁入口的步行路径不超过 300 米，显示到站步行、公交全程步行、共同乘车时间及预计到达时间
- 有效 Key + 前端 JS API Key + JS 安全密钥时：高德地图真实展示；无地图 Key 时显示示意路线
- 演示数据与真实数据均强制显著标注；真实候选均 **unverified / 待核实**
- 后端测试、Windows PowerShell 安装与启动脚本、Key 占位配置

**快速查看界面：** 在 Windows 上直接双击项目根目录的 `preview.html`，无需 Node.js、Python 或 Key。它是独立的模拟交互预览，不调用真实地图。

## Linux 服务器部署

在服务器普通 SSH 终端运行 `bash scripts/deploy-linux.sh`，默认监听 `0.0.0.0:8080`，构建前端并注册开机自启服务。缺少地图Key时使用演示模式。详细步骤与维护命令见 [Linux部署说明](docs/DEPLOY_LINUX.md)。

## 一、Windows 安装运行

已验证的用户环境：Windows PowerShell、Node.js v24.15.0、npm 11.12.1、Python 3.11.5、Git 2.33.1。

1. 解压本工程到如 `D:\Projects\AlongRoute` 或 `C:\Users\pc\Projects\AlongRoute`。
2. 在项目目录打开 PowerShell：

```powershell
cd "D:\Projects\AlongRoute"  # 按实际解压位置修改
powershell -ExecutionPolicy Bypass -File .\setup-windows.ps1
```

脚本将创建独立的 Python `.venv`、安装依赖、生成空白 Key 配置文件。**无需使用 `py`，也无需退出你的 Conda base 环境**。

3. 打开两个 PowerShell 窗口，分别执行：

**终端 1**

```powershell
powershell -ExecutionPolicy Bypass -File .\start-backend.ps1
```

**终端 2**

```powershell
powershell -ExecutionPolicy Bypass -File .\start-frontend.ps1
```

4. 打开 http://127.0.0.1:5173/。FastAPI 文档在 http://127.0.0.1:8000/docs。

**不配置高德 Key 也能看到完整的交互演示**；无需 Key 时不会发起真实地图规划。

## 二、仅由你本人完成的高德 Key 配置

详细步骤：[`docs/AMAP_KEYS_ONLY_YOU.md`](docs/AMAP_KEYS_ONLY_YOU.md)。

- `backend/.env` → `AMAP_WEB_SERVICE_KEY=...` （真正查询路线和地点）
- `backend/.env` → `AMAP_JS_SECURITY_CODE=...`（后端存放 JS 安全密钥）
- `frontend/.env.local` → `VITE_AMAP_JS_KEY=...`（前端公开的 JS API Key）

**不要将真实 Key 发送给任何聊天助手，或推送至 GitHub。** 提供的 `.gitignore` 已覆盖上述文件。

填好后重启前后端。Key 配置完成时，地图调用会产生真实配额消耗；实际公交换乘还依赖从高德地点搜索返回的 citycode。

## 三、API

| 方法 | URL | 说明 |
|---|---|---|
| GET | `/api/health` | 服务健康检查 |
| GET | `/api/config` | `demo` / `live` 模式和 JS 代理可用性 |
| GET | `/api/places/suggest?q=广州塔` | 地址建议（demo 返回少量演示 POI） |
| POST | `/api/recommend` | 计算下车点候选 |
| GET | `/docs` | Swagger/OpenAPI |

API 详细请求见 [`docs/API.md`](docs/API.md)。

## 四、算法边界与注意事项

- 司机绕路计算：`max(0, T(A→P) + T(P→B) - T(A→B)) + 90秒停靠预留`。绕路、共同乘车时间和地图司机路线采用相同的两个有向路段。演示与真实模式均保留 90 秒停靠预留，0 分钟限制下不返回候选。
- 沿司机原路线自适应选 6–12 个周边搜索中心，搜索停车场以及公交站/地铁站；另按约 200 米间隔生成道路候选，并在换乘站附近生成路线投影候选。最多详细评估 12 个点，粗筛兼顾乘客方向、到站距离和沿程覆盖，不保证全局最优。
- 从实际驾车路段名称识别高速/快速路、匝道、桥隧等限制线索，并排除已知转弯节点 50 米内及搜到的公交站 30 米内的候选。道路级别、完整路口、禁停标线、隔离设施、临时管制、桥上桥下与主辅路等信息并不完整，未识别出限制不等于允许临停，全部候选仍待现场核实。
- 公交站是换乘目标，不直接把站台作为私家车下客点。300 米限制针对所选公交方案的第一个实际公交站台或地铁入口，以步行接口核验；不会用直线距离、任意附近站点或地下车站中心替代。距离/入口/步行路径缺失或路由端点偏移过大时淘汰该公共交通方案。
- `max_walking_m` 仍控制公交全程步行（含换乘及末段），与固定的 300 米到站限制分别生效。全部公交备选先检查这些条件和换乘次数，再按偏好选择；单个公交方案查询失败不会抹去可用的打车方案。
- 乘客全程时间为 `T(A→P) + 停靠时间 + T(P→C)`，同时输出下车后耗时及预计到达。公交继续按全程时间和偏好评分；打车只用司机增加时间（含停靠）与预估车费筛选，保留最顺路、折中、最低车费等不同方案，乘客全程时间仅供参考。打车的“少绕路”优先司机增加时间，“省费用”优先车费，不再套用乘客耗时预算；未知费用不能作为省钱依据。
- 高德 POI **不等于安全合法的停车或上车点**。本 MVP 未取得行政许可/交通规则级停靠点数据源，全部候选标记为 `unverified`；用户须实地核实。
- 高德返回的 `taxi_cost` 是出租车估价，不是网约车实时叫车费用；驾车耗时不含打车等待和步行至上车点。
- 公交换乘使用预计抵达 P 的时间查询，公交时间也需要以实际运营为准。没有 citycode、没有相关线路或无返回值时不编造结果。
- 已兼容高德嵌套的轨迹字段；缺失轨迹时不编造连接线。真实地图中的 S 标记为所选公共交通的上车站台或地铁入口，紫色路线为到站步行。
- 真实请求可能并发消耗较多地图服务配额，**上线前应加限流、Redis 缓存、预算上限、请求日志脱敏和授权检查**。
- 本地后端已按接口共享请求节流，QPS 超限时最多尝试 3 次，并对同一次规划内相同请求合并复用；完整推荐可能需要几十秒至一两分钟。接口失败会提示错误或结果不完整。该进程内节流不能替代生产部署的跨进程限流与配额管理。
- 开发环境的 JS 安全代理仅用于本机调试，公网部署必须收紧允许路径并限流，不要直接公开当前的开放代理。
- 目前没有用户认证、生产数据库、真实网约车实时订单、停车监管数据，也没有对不同实际地点进行导航实测。

## 五、运行测试

```powershell
cd backend
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
$env:PYTHONPATH = '.'
.\.venv\Scripts\python.exe -m pytest -q
```

前端构建：

```powershell
cd frontend
npm run build
```

## 六、项目结构

```text
AlongRoute/
├─ backend/
│  ├─ app/{main.py,models.py,geometry.py,optimizer.py,demo.py,amap.py}
│  ├─ tests/
│  ├─ .env.example
│  └─ requirements.txt
├─ frontend/
│  ├─ src/{App.vue,style.css,components/,lib/,types/}
│  ├─ .env.example
│  └─ package.json
├─ docs/{API.md,AMAP_KEYS_ONLY_YOU.md}
├─ setup-windows.ps1
├─ start-backend.ps1
└─ start-frontend.ps1
```

## 七、地图服务官方资料

- [高德路径规划 2.0](https://developer.amap.com/api/webservice/guide/api/newroute)
- [高德 POI 搜索 2.0](https://developer.amap.com/api/webservice/guide/api-advanced/newpoisearch)
- [高德输入提示](https://developer.amap.com/api/webservice/guide/api-advanced/inputtips)
- [高德 JS API 安全密钥](https://developer.amap.com/api/javascript-api-v2/guide/abc/jscode)

许可证：本工程示例代码可供你自行修改使用；所依赖的第三方服务和数据仍受各自服务条款限制。


最新算法、限制及验证见 [算法改造记录](docs/algorithm-upgrade.md)。本版本用于出发前规划；司机增加时间上限包含停靠，打车包含明确标注的5分钟接车假设，实际等待未接入。

## 本地搜索日志

路线规划和地点名称搜索均生成独立 `search_id`，响应头为 `X-Search-ID`，路线结果同时返回 `search_id` 并在页面显示。日志位于项目根目录 `.runtime/search-logs/YYYY-MM-DD.jsonl`（中国时间），每行一个 JSON 事件，可按搜索编号筛选。单个文件到10MB后轮换，保留当前文件及至多3个轮换备份；按日新建文件，日志目录不提交 Git。

记录搜索输入、基准路线摘要、地图接口耗时、候选点和拒绝原因、打车淘汰依据及替代候选、最终推荐和总耗时；成功、参数错误、服务错误、超时、取消均有结束状态。地点搜索记录关键词和结果数量。不会记录地图密钥或完整地图请求 URL，路线轨迹也不重复写入日志。日志写入失败会提示并继续返回规划结果。日志从功能启用后开始，不补录之前的搜索。

### 可视化候选测试流程

在“自动推荐”结果区展开“候选测试流程 · 可视化回放”，加载本次搜索，依次点击“搜索出口 → 生成候选 → 名额预选 → 路线核验 → 最终推荐”。图中点位与候选列表联动，支持按名称、坐标、来源和原因筛选；点击点位查看出口约束、分流方向、近中远采样、拒绝原因及打车评分。灰色名额淘汰和预算内未完成核验不代表路线不可行。

也可以输入旧搜索编号复盘，例如清湖对照行程 `ab72fc02dff441a1bb72a41577747df8`（仅在本机该日志仍保留时可用）。回放只读本地日志，不调用地图规划接口。新搜索记录简化探索路线和出口查询范围；旧日志仅显示已有点位，不补造轨迹。示意图不是道路地图，核验通过也不等于停靠许可。日志编号可用于读取对应行程，请仅向需要排查问题的人分享。

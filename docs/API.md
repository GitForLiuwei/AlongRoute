# AlongRoute REST API

所有地点坐标为 `lon,lat` 经度在前，纬度在后；使用高德地点搜索结果中的 GCJ-02 坐标。后端采用 Pydantic 做数据校验。

## POST `/api/recommend`

```json
{
  "origin": {"name": "广州塔", "point": {"lon": 113.3245, "lat": 23.1065}, "citycode": "020"},
  "driver_destination": {"name": "广州东站", "point": {"lon": 113.3279, "lat": 23.1370}, "citycode": "020"},
  "passenger_destination": {"name": "广州南站", "point": {"lon": 113.2690, "lat": 22.9890}, "citycode": "020"},
  "preferences": {
    "max_detour_minutes": 5,
    "max_taxi_fare_yuan": 200,
    "modes": ["taxi", "transit"],
    "priority": "balanced",
    "max_walking_m": 1200,
    "max_transfers": 2
  },
  "departure_time": "2026-11-04T10:00:00+08:00"
}
```

`departure_time` 可不传，系统采用当前中国时区时间。响应结构：

公共交通有固定的 **300 米到站步行上限**，不由 `max_walking_m` 调整。后者控制公交全程步行。仅保留步行接口核验至所选方案实际首乘站台/地铁入口不超过 300 米的方案；不能核验时淘汰该公交方案。

`max_detour_minutes` 支持0–180分钟（3小时），含额外驾驶和停靠；默认5分钟。`max_taxi_fare_yuan` 为乘客下车后前往目的地的打车估价上限，支持0–200元，默认200元；等于上限可通过，超过上限或费用未知的打车选项不推荐。该上限不约束公交票价，混合模式仍可保留同一点符合限制的公交选项。

寻找下车候选时，先从地图返回的最多三条司机A→B路线中选最快路线作为基准；用于发现候选的备选路线，最多允许比基准慢 `max(0, max_detour_minutes - 10)` 分钟，等于边界也保留。绕路上限不超过10分钟时，仅保留与最快路线同耗时的路线。具体A→P→B方案仍按完整司机增加时间（含停靠）执行页面所设上限。

当A→B探索路线包含高速、快速路或高架时启用两批出口候选搜索。第一批逐条找到距C直线最近的点Q，Q在受控路段时沿行驶顺序前后发现各最多三个出口；Q在普通路段时保留普通道路候选。第二批额外查询A→C的最多两条探索路线，从其中受控道路沿线寻找靠近C的出口，每条最多三个不同出口区域；即使A→C终点C在普通道路上，也搜索其此前的受控路段出口。

每个出口先核验A直接经该出口到B的实际驶出路线，再计算出口外普通道路到B/C的路线。比较两条路线从起点连续重合的共同前缀，在首次分流前及两条分支上生成普通道路候选；之后的再次相交不算共同前缀。Q仅用于搜索定位，不是司机途经点，Q之前的出口可提前驶出。A→C路线仅用于发现候选，不替代A→B最快基准；所有方案仍按完整A→P→B的司机增加时间和P→C费用执行硬上限及原综合评分。

公交/地铁站在分流附近候选周围5公里范围召回，并以驶出后的实际驾车路线生成站旁普通道路下车点。5公里是站点召回范围，实际方案首乘站台/入口的核验步行仍不超过300米。站点按区域和公交/地铁类别轮流分配最多12个精算名额。所有候选最后仍以连续A→接近点→出口外道路→P→B核验驶出顺序、道路条件及司机增加时间；费用与排序规则保留。

出口分支的预选在并发完成后按路线编号、区域沿程位置、出口约束及坐标稳定整理。分流前、去B和去C三个方向分别在近（15/30/60/80米）、中（150/200/300/400米）、远（600/800/1000/1200米）层次寻找普通道路点，每层最多一个；范围内无合格点时不回退到其他层。同一方向仅用第一个合格代表点扩展站点搜索，避免随采样层数放大站点查询。

含高速的路线也从已取得的原路线普通道路几何补充候选，不增加地点发现接口调用。初始最多48个候选，其中最多预留12个普通道路候选名额，不足时回填；其余名额保持司机/乘客方向出口及近站候选交错覆盖。同坐标、同接近点和同出口约束合并交通方式与来源；不同出口约束及仅位置接近的点不会合并。以上名额和采样距离为实验参数，不构成最优性保证。公交单模式达到六个可行点后，还需启动过各已入选区域的代表候选，才允许提前结束；阶段总时间预算仍生效。

新增日志 `candidate_generated`、`candidate_preselection`（`merged/selected/quota_limit`）、`preselection_summary`，记录候选键、路线、区域、分支、采样层次及来源。汇总中的 `primary_only_displaced` 表示与本次“不预留普通道路名额”的候选名单相比被替换的点，并非完整旧算法结果。精算开始、接受、拒绝事件携带相同定位字段；`candidate_not_evaluated` 区分已入选但因提前停止或阶段预算未启动的候选。


出口及分流搜索最多38秒，最多24个出口查询中心，其中为A→B和A→C分别预留最多16和8个名额。两批道路候选、两批站旁候选分别交替合并，初始精算最多48个，避免一条路线或一类交通方式占满名额。未找齐出口、站点查询失败或达到预算时，`search_complete=false`并附提示，不放宽约束。A→C查询失败时仍保留A→B搜索；当A→B各路线均无受控路段时，沿用普通道路及A→C广域探索。

普通国道、省道（如G107、S359）允许进入候选，明确的高速、快速路、匝道、桥隧等路段仍排除；导航指引仅提及高速不将明确的普通国省道路段归为高速。普通道路点不再仅因50米范围内有高速几何而整体排除，仍要求实际路线连接及5米内具名道路信息，匹配受限路段时淘汰。所有停靠状态保持unverified，道路编号不构成停车认证。

`duration_s` 表示下车后的耗时；`total_duration_s` = `shared_ride_s + stop_duration_s + duration_s`，用于预计到达、打车和公交评分。真实打车方案的 `duration_s` 包含明确标注的5分钟接车缓冲假设，不是实际等车预测。

打车先执行司机绕路与乘客费用硬上限，再按司机增加时间、乘客全程时间和费用三维淘汰被全面超过的候选。统一综合分为 `driver_detour_s / 60 + total_duration_s / 60 + fare_yuan * 3`，越低越优。司机与乘客的时间等权相加：司机省1分钟、乘客省2分钟合计省3分钟，值得多花1元；恰好等价时优先省时方案。使用十进制费用计算避免浮点误差影响边界。

按综合分返回最多六个候选，标签为综合推荐及综合备选，`score` 为上述综合分，不能与公交分数比较。取消原先只比较司机绕路与车费的二维淘汰、多绕1分钟省3元的独立规则，以及最顺路/最低价端点优先展示；乘客共同乘车、停靠、接车缓冲及后续行程均参与打车排名。未知车费不参与推荐。`priority` 只控制公交推荐，打车始终按1元折合3分钟的取舍排序。混合模式仍分别筛选再交错展示，合并同一下车点，总数不超过6个。

公交站台附近的位置可以进入候选筛选，不再因距公交站台30米以内而自动淘汰；公交接驳仍核验真实步行路径及300米上限，驾车道路条件及司机增加时间仍需通过检查。候选的停靠状态继续为 `unverified`。

转弯、掉头及环岛节点附近50米以内的位置也可以进入候选筛选，不再按距这些节点的距离自动淘汰。

`baseline_route` 和每个推荐的 `driver_route` 同时输出司机原路线与经停路线。路线包含 `distance_m`（完整驾驶里程）、`duration_s`（纯驾驶耗时，不含停靠）及按行驶顺序的 `driving_steps`：`road_name`、`instruction`、`action`、`assistant_action`、`distance_m`、`duration_s`。缺失路段距离或耗时为 `null`，缺失道路名称和指引为空字符串，不编造导航。司机经停总耗时为 `driver_route.duration_s + stop_duration_s`；下车后司机剩余驾驶时间为 `driver_route.duration_s - shared_ride_s`。前端展示里程差、原路线与经停方案对比，复制行程时附带司机路线。

每个推荐的 `point.point` 是实际用于途经算路与后续交通规划的 GCJ-02 经纬度；`dropoff_road` 从已核验驾车路线中距该点5米内的普通道路路段提取，缺失为 `null`。道路采样点的展示名使用道路名称并标为下车候选，不伪造门牌、场所入口或可停车认证。卡片与司机、乘客详情显示该点坐标，与地图 P 标记一致。

```json
{
  "mode": "demo",
  "baseline_route": {"duration_s": 600, "distance_m": 5000, "polyline": [], "fare_yuan": null},
  "recommendations": [
    {
      "id": "...", "point": {"name": "...", "point": {"lon": 113.3, "lat": 23.1}},
      "driver_detour_s": 180,
      "shared_ride_s": 300, "stop_duration_s": 90, "candidate_source": "roadside",
      "driver_route": {"duration_s": 690, "distance_m": 5300, "polyline": []},
      "options": [
        {"mode": "transit", "duration_s": 900, "total_duration_s": 1290,
         "arrival_at": "2026-11-04T10:21:30+08:00", "fare_yuan": 5.0, "walking_m": 600,
         "boarding_walk_m": 200, "boarding_kind": "subway",
         "boarding_stop": {"name": "示例站A口", "point": {"lon": 113.301, "lat": 23.1}},
         "boarding_walk_route": [], "transfers": 0, "details": "...", "route": [], "notes": []}
      ],
      "suggested_mode": "transit", "score": 42.7,
      "stop_status": "unverified", "stop_warning": "...", "reason": "..."
    }
  ],
  "warnings": ["..."], "generated_at": "2026-11-04T02:00:00+00:00",
  "examined_candidates": 12, "station_walk_limit_m": 300
}
```

以上是**结构示例**，不是实际一次请求的返回数据。`unverified` 表示未经合法停车核实，地图上只能查看候选地点，不能作为立即停车指令。

## POST `/api/evaluate-dropoff`：手动指定下车点测试

请求沿用 `/api/recommend` 的A/B/C、preferences及departure_time，额外必填 `dropoff`（与origin相同的Place结构）。需要真实地图配置，不返回虚构演示结果。

直接查询A→B、A→P→B及P→C，沿用24小时持久缓存，不执行候选发现、道路筛除或费用/绕路超限淘汰。返回原路线与经停路线、`driver_detour_s`（额外驾驶加90秒停靠）、`fare_yuan`、`passenger_total_s`（到P共同乘车加90秒停靠、300秒接车及P→C驾驶）、`combined_score`，以及`within_detour_limit`、`within_taxi_fare_limit`、`road_condition_usable`。

超出限制仍展示测算值；道路条件不明附提示。后续打车路线失败时保留司机结果，将乘客全程与费用置为null；费用未知时预算核验及综合分为null。无法取得到P的连续路线和耗时则返回502。支持取消，搜索日志类型为manual，包含manual_input/manual_result事件。页面“测试模式 · 指定下车点”提供地点搜索和三项结果及时间拆分。

## GET `/api/places/suggest?q=广州`

返回 `items`，每项包括 `name`、`address`、`point`、`citycode`、`poi_id`；没有 Key 时只搜索内置演示地点。

## GET `/api/config`

返回 `mode`（demo/live）、`map_proxy_ready`（JS 安全密钥是否已配置），不泄露密钥。

## GET `/api/health`

返回 `{"status":"ok","service":"AlongRoute"}`。

## 搜索诊断日志

`POST /api/recommend` 与 `GET /api/places/suggest` 的响应头包含 `X-Search-ID`，规划成功响应另有 `search_id` 字段。`X-Search-Log-Status: unavailable` 表示日志写入失败，规划仍可正常返回。参数错误也有编号和验证失败事件；日志不会存储未经验证的完整请求体。

每个编号关联 `.runtime/search-logs/YYYY-MM-DD.jsonl` 中的事件，含中国时间 `time`、搜索类型 `kind`、事件序号 `sequence`、从开始累计的 `elapsed_ms` 与 `event`。常见事件：`search_started`、`route_input`、`baseline_ready`、`map_request_finished`、`candidates_ready`、`candidate_accepted`、`candidate_rejected`、`taxi_filtered`、`taxi_shortlist`、`route_result`、`search_finished`。结束状态为 `success/error/timeout/cancelled`，成功但搜索不完整时以结果的 `warnings` 与 `search_complete` 判断。

`destination_corridors_ready`记录乘客方向探索路线数量，`discovery_coverage`记录本次实际出口查询中心、周边查询中心及保留的出口锚点；`candidate_pool_created`和`candidates_ready`的`discovery_group`区分baseline与destination，以便查明某区域是否搜索或被候选配额挤出。

例如 `taxi_filtered` 的 `reason=dominated` 会记录被淘汰点与替代点ID、司机增加时间、乘客全程时间和车费；`taxi_shortlist` 记录综合排序及每元折合180秒的权重。`candidate_rejected` 的原因包括 `detour_limit`、`unusable_or_unknown_road`、`missing_continuous_driver_route`、`exit_not_reached_in_order`、`no_viable_onward_options`、`map_service_error`、`evaluation_cancelled_or_budget_exhausted`。日志包含地点与坐标，不包含地图密钥、原始请求 URL 或完整轨迹。

## 地图API缓存

测试阶段，后端通过本地SQLite文件 `.runtime/api-cache.sqlite3` 持久缓存高德成功响应：地点搜索、周边搜索、步行路线、驾车路线、公交路线统一缓存24小时。缓存键包含接口、完整参数（公交含出发日期和分钟）及密钥隔离信息的哈希；不会把密钥写入缓存元数据或日志。请求失败、限流错误和取消不会缓存；命中不占用本次规划的250次外部接口预算，日志 `map_request_finished.cache=hit` 可查看命中。

缓存最多512条、响应数据32MiB，超出后按最近最少使用淘汰，过期自动失效；后端或电脑重启不清空，继续使用原有效期，命中不延长24小时期限。磁盘暂不可用时记录告警并降级为内存缓存。缓存返回数据副本，每次规划仍重新执行当前费用与绕路限制，结果时间及搜索编号重新生成；接口缓存不代表实时车辆供给或停车核实。

### 候选测试回放

`GET /api/search-replay/{search_id}`：按完整32位搜索编号读取自动规划日志，返回 `{search_id, finished, events}`。不提供搜索历史列表；返回禁止缓存，只输出候选诊断事件，地图接口请求记录不外传。无此日志返回404，非法编号返回422，读取失败返回503。残缺日志可回放，`finished=false` 表示尚未看到结束事件。文件轮换清理后无法恢复；地点搜索和手动测算不包含自动候选流程。

`discovery_map` 事件记录出口查询中心、1500米查询半径、各条司机/乘客探索路线最多200个简化点。仅新增搜索包含此事件；轨迹用于空间理解，不作为导航。候选按完整的“坐标＋接近点＋出口约束”区分；相同坐标的不同约束可重叠。交通方式淘汰原因单独显示，不能将某一种方式淘汰解释为整个点不可用。

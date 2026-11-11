# AlongRoute REST API

所有地点坐标为 `lon,lat` 经度在前，纬度在后；使用高德地点搜索结果中的 GCJ-02 坐标。后端采用 Pydantic 做数据校验。

## POST `/api/recommend`

```json
{
  "origin": {"name": "广州塔", "point": {"lon": 113.3245, "lat": 23.1065}, "citycode": "020"},
  "driver_destination": {"name": "广州东站", "point": {"lon": 113.3279, "lat": 23.1370}, "citycode": "020"},
  "passenger_destination": {"name": "广州南站", "point": {"lon": 113.2690, "lat": 22.9890}, "citycode": "020"},
  "preferences": {
    "max_detour_minutes": 120,
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

公共交通有固定的 **300 米到站步行上限**，不由 `max_walking_m` 调整。后者控制公交全程步行及直接步行回家的距离上限。仅保留步行接口核验至所选方案实际首乘站台/地铁入口不超过 300 米的方案；不能核验时淘汰该公交方案。

`max_detour_minutes` 支持0–120分钟（2小时），含额外驾驶和停靠；默认120分钟。`max_taxi_fare_yuan` 为乘客下车后前往目的地的打车估价上限，支持0–200元，默认200元；等于上限可通过，超过上限或费用未知的打车选项不推荐。该上限不约束公交票价，混合模式仍可保留同一点符合限制的公交选项。

方案中的司机总时间（含停靠）为 `driver_route.duration_s + stop_duration_s`，表示从 A 经 P 到 B 的完整驾驶及停靠时间。

寻找下车候选时，先从地图返回的最多三条司机A→B路线中选最快路线作为基准；用于发现候选的备选路线，最多允许比基准慢 `max(0, max_detour_minutes - 10)` 分钟，等于边界也保留。绕路上限不超过10分钟时，仅保留与最快路线同耗时的路线。具体A→P→B方案仍按完整司机增加时间（含停靠）执行页面所设上限。

默认策略为 `hub_first`：先查询最多两条 A→C 探索路线，与保留的 A→B 路线共同确定最多六个召回中心（C、路线近 C 位置、沿线出口及采样位置），在每个中心周围 5 km 搜索停车场、地铁站，公交模式另搜公交站。优先复用 `.runtime/hub-pool.sqlite3` 的空间候选池（默认有效期30天）；候选种子不是可以直接停车的位置。

种子按区域和类型保留覆盖，最多 96 个参加批量粗排，再对最多 18 个枢纽规划真实驾车接近路线。核心圈为3000米，地铁种子按中心东西两侧分组；末端存在掉头时优先生成掉头前的同侧道路点。每个枢纽最多生成两个具名普通道路下客点，第一个允许距锚点450米、第二个300米内；另可补充驶出高速后的首段普通道路点。不能证明道路可用时不直接拿站点中心替代。300 m 此处只用于生成候选，正式公交仍须通过真实入口步行核验。

配置 `OSRM_BASE_URL` 后使用本地有向稀疏矩阵计算 A→B、A→P、P→B、P→C；坐标只在 OSRM 边界由 GCJ-02 转为 WGS-84。未配置、超时、空值、过远吸附或不支持距离字段时，回退到几何预排序。两种估计都只决定先核验谁，不用于认定路线可行或执行硬淘汰；出租车估算仅是内部粗排参数，正式费用仍来自高德。公交枢纽使用独立优先级和覆盖顺序，避免被 P→C 汽车耗时压掉。

普通道路由 A→B 与 A→C 的实际路线补充，不要求靠近站点。枢纽道路不足八个，或公交模式缺少站旁道路候选，且 A→B 含受控路段时，才启用最多八秒的出口分流补充：最多六个查询中心、每条路线最多两个出口，不再重复站点搜索或 A→C 查询。整体发现预算为 38 秒。

最终候选再次粗排，并按来源分配最多 48 个精算名额，普通道路最多预留 12 个，空缺回填。每批精算八个；已核验至少 24 个、取得至少六个经排名去重后的独立推荐、覆盖请求的交通方式和已入选区域时可提前停止，未满足则继续至候选或阶段预算用尽。只返回完整通过高德路线、费用、绕行与公交接驳核验的方案。

P到C直线距离不超过800米时，先核验真实步行路线；路线长度不超过 `min(1200, max_walking_m)` 才返回“¥0 · 直接步行回家”。该选项的 `direct_walk=true`，`mode` 沿用所选交通分组以兼容现有接口，费用和接车等待均为0，`route`、`duration_s`、`walking_m`、`arrival_at` 使用实际步行数据；不应显示打车或公交行程。步行查询失败时保留原交通查询流程。目的地门口绕行超过15分钟且存在少绕行至少3分钟的替代点时，剔除该门口候选。

公交方案预计到达P的中国时间落在22:15（含）至次日05:50（不含）时，`details` 附加末班车提醒；直接步行方案不提示。已有六个排名后候选时，若新点司机增加时间比其中最大值还多25分钟，会跳过接驳查询，并将 `search_complete` 标为false。打车评分对司机增加超过18分钟的部分追加75%惩罚，并可将更少绕行的帕累托折中点提升至第二位。

所有 P 均验证完整 A→P→B；出口约束候选验证 A→接近点→出口外道路→P→B。候选键包含 P、接近点及出口约束；不同路径不因同一个 P 就合并时间和交通方式。推荐 ID 是候选身份的哈希，不再复用 POI ID。无约束的枢纽/道路方案从最终实际路线识别驶出位置。

枢纽搜索受中心、POI 页数和候选名额限制，`search_complete=false`，不表示所有候选都失败。日志 `hub_seeds`、`hub_shortlist`、`candidate_coarse`、`hub_exit_supplement` 记录召回和估算；`candidate_generated`、`candidate_preselection`、`preselection_summary` 继续记录实际 P 的名额分配。回放显示枢纽锚点、实际下客点和粗排来源。

设置 `CANDIDATE_STRATEGY=legacy` 可恢复此前 Q 前后出口/首次分流搜索用于对照，默认无需设置。`OSRM_DATA_VERSION` 可记录本地路网及 profile 版本；本次改造不自动安装 OSRM 或下载路网。

普通国道、省道（如G107、S359）允许进入候选，明确的高速、快速路、匝道、桥隧等路段仍排除；导航指引仅提及高速不将明确的普通国省道路段归为高速。普通道路点不再仅因50米范围内有高速几何而整体排除，仍要求实际路线连接及5米内具名道路信息，匹配受限路段时淘汰。所有停靠状态保持unverified，道路编号不构成停车认证。

`duration_s` 表示下车后的耗时；`total_duration_s` = `shared_ride_s + stop_duration_s + duration_s`，用于预计到达、打车和公交评分。真实打车方案的 `duration_s` 包含明确标注的5分钟接车缓冲假设，不是实际等车预测。

打车先执行司机绕路与乘客费用硬上限，再按司机增加时间、乘客全程时间和费用三维淘汰被全面超过的候选。综合分按 `priority` 切换（所有时间单位为分钟）：`balanced` 为 `1.5×司机增加 + 乘客全程 + 1.8×车费`；`time` 为 `1.5×司机增加 + 1.5×乘客全程 + 0.8×车费`；`cost` 为 `1.2×司机增加 + 0.8×乘客全程 + 3.0×车费`。同分优先双方合计耗时更短的方案，使用十进制费用计算避免边界误差。

按综合分选出最多六个候选，同一具名道路上相距不超过2.5公里的不同位置只保留综合分最优的代表；相距不超过1.5公里、车费差不超过6元、司机增加时间差不超过7分钟、乘客全程差不超过6分钟的不同位置归并为同片区。`score` 为综合分，不能与公交分数比较。候选若司机增加超过90秒，且乘客全程与费用均不优于“随车到B后前往C”的同方式对照，则在最终短名单中置后；费用未知时不作此判断。未知打车费不参与推荐。公交省时间、省费用模式额外保留 `0.35×司机增加分钟` 权重。

混合模式仍分别筛选再交错展示，仅合并下车点及司机实际路线、到达时间一致的选项，总数不超过6个。

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

直接查询A→B、A→P→B及P→C，沿用按接口时效的持久缓存，不执行候选发现、道路筛除或费用/绕路超限淘汰。返回原路线与经停路线、`driver_detour_s`（额外驾驶加90秒停靠）、`fare_yuan`、`passenger_total_s`（到P共同乘车加90秒停靠、300秒接车及P→C驾驶）、`combined_score`（按请求的 `preferences.priority` 使用相同打车权重），以及`within_detour_limit`、`within_taxi_fare_limit`、`road_condition_usable`。

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

测试阶段，后端通过本地SQLite文件 `.runtime/api-cache.sqlite3` 持久缓存高德成功响应：地点搜索、周边搜索、步行路线缓存24小时，驾车路线缓存180秒，公交路线缓存300秒。缓存键包含当前接口TTL策略、接口、完整参数（公交含出发日期和分钟）及密钥隔离信息的哈希；不会把密钥写入缓存元数据或日志。请求失败、限流错误和取消不会缓存；命中不占用本次规划的250个未命中缓存的逻辑请求预算（限流重试会产生额外HTTP尝试），日志 `map_request_finished.cache=hit` 可查看命中。

缓存最多512条、响应数据32MiB，超出后按最近最少使用淘汰，过期自动失效；后端或电脑重启不清空，继续使用原有效期，命中不延长各接口的原有效期；TTL策略变更后旧缓存键失效。磁盘暂不可用时记录告警并降级为内存缓存。缓存返回数据副本，每次规划仍重新执行当前费用与绕路限制，结果时间及搜索编号重新生成；接口缓存不代表实时车辆供给或停车核实。

### 候选测试回放

`GET /api/search-replay/{search_id}`：按完整32位搜索编号读取自动规划日志，返回 `{search_id, finished, events}`。不提供搜索历史列表；返回禁止缓存，只输出候选诊断事件，地图接口请求记录不外传。无此日志返回404，非法编号返回422，读取失败返回503。残缺日志可回放，`finished=false` 表示尚未看到结束事件。文件轮换清理后无法恢复；地点搜索和手动测算不包含自动候选流程。

`discovery_map` 事件记录出口查询中心、1500米查询半径、各条司机/乘客探索路线最多200个简化点。仅新增搜索包含此事件；轨迹用于空间理解，不作为导航。候选按完整的“坐标＋接近点＋出口约束”区分；相同坐标的不同约束可重叠。交通方式淘汰原因单独显示，不能将某一种方式淘汰解释为整个点不可用。

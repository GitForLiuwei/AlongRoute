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
    "max_walking_m": 3000,
    "max_transfers": 4
  },
  "departure_time": "2026-11-04T10:00:00+08:00"
}
```

`departure_time` 可不传，系统采用当前中国时区时间。响应结构：

公共交通优先选择到站步行 300 米以内的方案，最多允许 **480 米**，不由 `max_walking_m` 放宽。超出 300 米的部分按 `超出米数 / 1.25 × 2.5` 秒增加评分惩罚，不修改实际到达时刻。`max_walking_m` 控制公交全程步行及直接步行回家的距离上限；实际首乘站台 / 地铁入口无法核验时淘汰该公交方案。

`max_detour_minutes` 支持0–120分钟（2小时），含额外驾驶和停靠；默认120分钟。`max_taxi_fare_yuan` 为乘客下车后前往目的地的打车估价上限，支持0–200元，默认200元；等于上限可通过，超过上限或费用未知的打车选项不推荐。该上限不约束公交票价，混合模式仍可保留同一点符合限制的公交选项。

方案中的司机总时间（含停靠）为 `driver_route.duration_s + stop_duration_s`，表示从 A 经 P 到 B 的完整驾驶及停靠时间。

当前默认流程为枢纽优先召回、普通道路补充、可选 OSRM 粗排和高德路线核验，按打车 / 公交分别展示最多六张方案卡片。每张卡片的 `options` 只保留对应方式，客户端以 `(id, suggested_mode)` 区分同点位的多种方案。直接步行使用 `direct_walk=true`，费用为零，不应显示车辆接车缓冲。

当前初始候选上限为 24 个，普通道路预留 6 个；每批执行 6 个，满足至少 12 个候选、四个独立司机行程及交通方式最低数量后可早停。部分 `detour_limit` 和 `detour_bound_pruned` 发生在完整路线请求之前，不表示已核验道路不可达。完整的召回范围、剪枝条件、评分公式和限制见 [选点与推荐算法](PLANNING.md)。

`duration_s` 是下车后耗时，`total_duration_s = shared_ride_s + stop_duration_s + duration_s` 是乘客全程耗时。真实打车的 `duration_s` 包含固定五分钟接车缓冲假设。两种交通方式的评分口径不同，不能直接按分数比较。

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
  "examined_candidates": 12, "station_walk_limit_m": 480
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

`POST /api/recommend` 与 `GET /api/places/suggest` 的响应头包含 `X-Search-ID`，规划成功响应另有 `search_id` 字段。自动推荐可携带客户端生成的 32 位十六进制 `X-Search-ID` 请求头，服务端绑定同一编号；无效或已使用的编号不会复用旧搜索。`X-Search-Log-Status: unavailable` 表示日志写入失败，规划仍可正常返回。参数错误也有编号和验证失败事件；日志不会存储未经验证的完整请求体。

每个编号关联 `.runtime/search-logs/YYYY-MM-DD.jsonl` 中的事件，含中国时间 `time`、搜索类型 `kind`、事件序号 `sequence`、从开始累计的 `elapsed_ms` 与 `event`。常见事件：`search_started`、`route_input`、`baseline_ready`、`map_request_finished`、`candidates_ready`、`candidate_accepted`、`candidate_rejected`、`taxi_filtered`、`taxi_shortlist`、`route_result`、`search_finished`。结束状态为 `success/error/timeout/cancelled`，成功但搜索不完整时以结果的 `warnings` 与 `search_complete` 判断。

`destination_corridors_ready`记录乘客方向探索路线数量，`discovery_coverage`记录本次实际出口查询中心、周边查询中心及保留的出口锚点；`candidate_pool_created`和`candidates_ready`的`discovery_group`区分baseline与destination，以便查明某区域是否搜索或被候选配额挤出。

例如 `taxi_filtered` 的 `reason=dominated` 会记录被淘汰点与替代点ID、司机增加时间、乘客全程时间和车费；`taxi_shortlist` 记录综合排序及每元折合180秒的权重。`candidate_rejected` 的原因包括 `detour_limit`、`unusable_or_unknown_road`、`missing_continuous_driver_route`、`exit_not_reached_in_order`、`no_viable_onward_options`、`map_service_error`、`evaluation_cancelled_or_budget_exhausted`。日志包含地点与坐标，不包含地图密钥、原始请求 URL 或完整轨迹。

## 地图API缓存

测试阶段，后端通过本地SQLite文件 `.runtime/api-cache.sqlite3` 持久缓存高德成功响应：地点搜索、周边搜索、步行路线缓存24小时，驾车路线缓存180秒，公交路线缓存300秒。缓存键包含当前接口TTL策略、接口、完整参数（公交含出发日期和分钟）及密钥隔离信息的哈希；不会把密钥写入缓存元数据或日志。请求失败、限流错误和取消不会缓存；命中不占用本次规划的250个未命中缓存的逻辑请求预算（限流重试会产生额外HTTP尝试），日志 `map_request_finished.cache=hit` 可查看命中。

缓存最多512条、响应数据32MiB，超出后按最近最少使用淘汰，过期自动失效；后端或电脑重启不清空，继续使用原有效期，命中不延长各接口的原有效期；TTL策略变更后旧缓存键失效。磁盘暂不可用时记录告警并降级为内存缓存。缓存返回数据副本，每次规划仍重新执行当前费用与绕路限制，结果时间及搜索编号重新生成；接口缓存不代表实时车辆供给或停车核实。

### 候选测试回放

`GET /api/search-replay/{search_id}`：按完整32位小写十六进制搜索编号读取自动规划日志，优先读取内存活动 Trace，未命中时读取轮换文件，返回 `{search_id, finished, events}`。不提供搜索历史列表；返回禁止缓存，只输出候选诊断事件，地图接口请求记录不外传。无此日志返回404，非法编号返回422，读取失败返回503。残缺日志可回放，`finished=false` 表示尚未看到结束事件。文件轮换清理后无法恢复；地点搜索和手动测算不包含自动候选流程。

`discovery_map` 事件记录出口查询中心、1500米查询半径、各条司机/乘客探索路线最多200个简化点。仅新增搜索包含此事件；轨迹用于空间理解，不作为导航。候选按完整的“坐标＋接近点＋出口约束”区分；相同坐标的不同约束可重叠。交通方式淘汰原因单独显示，不能将某一种方式淘汰解释为整个点不可用。


## GET `/api/search-replay/{search_id}/stream`

当前自动规划的 Server-Sent Events 通道，返回 `Content-Type: text/event-stream`、`Cache-Control: no-store` 和 `X-Accel-Buffering: no`。连接可以先于或晚于对应的 POST 建立；先发送已有事件快照，再继续推送新事件。

普通帧使用默认 `message` 事件，`data` 为 JSON，包含 `event`、`sequence`、`elapsed_ms` 和该事件允许公开的候选字段。常用事件包括 `candidate_generated`、`candidate_started`、`candidate_accepted`、`candidate_rejected`、`search_finished`。内部地图请求和任意附加字段不向回放客户端公开。

```text
data: {"event":"candidate_generated","sequence":2,"elapsed_ms":120,"candidate_key":["113.3,23.1","",""]}

data: {"event":"search_finished","sequence":8,"elapsed_ms":1500,"status":"success"}

event: done
data: {"finished": true}

```

流空闲时发送注释心跳，最长保持 120 秒；断开、取消和关闭时退订。非法编号返回 422，进程内注册表容量不足时返回 503。当前通道使用单 worker 内存状态，不用于恢复任意历史日志；历史搜索使用 JSON 回放接口。

前端在 POST 完成后立即展示最终方案，SSE 失败不影响结果；完整过程可手动重播。更多说明见 [实时进度与回放](SEARCH_REPLAY.md)。

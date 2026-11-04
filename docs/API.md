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

`duration_s` 表示下车后的耗时；`total_duration_s` = `shared_ride_s + stop_duration_s + duration_s`，用于评分与预计到达。后续打车耗时仍不包含叫车等待。

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

## GET `/api/places/suggest?q=广州`

返回 `items`，每项包括 `name`、`address`、`point`、`citycode`、`poi_id`；没有 Key 时只搜索内置演示地点。

## GET `/api/config`

返回 `mode`（demo/live）、`map_proxy_ready`（JS 安全密钥是否已配置），不泄露密钥。

## GET `/api/health`

返回 `{"status":"ok","service":"AlongRoute"}`。

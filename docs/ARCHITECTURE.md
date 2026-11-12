# 项目结构与代码阅读顺序

```text
AlongRoute/
├─ backend/
│  ├─ app/                  # API、候选搜索、地图适配、核验与排名
│  ├─ tests/                # 离线回归测试
│  ├─ .env.example
│  ├─ requirements.txt
│  └─ requirements-dev.txt
├─ frontend/
│  ├─ src/
│  │  ├─ App.vue            # 输入、请求快照、候选与交通方式切换
│  │  ├─ components/       # 地图、行程、手动测算、候选回放
│  │  └─ lib/api.ts         # API 类型与请求封装
│  ├─ .env.example
│  └─ package.json
├─ docs/                    # API、部署、Key 配置和历史方案
├─ scripts/                 # Linux 部署及诊断脚本
├─ .runtime/                # 本地运行产物、缓存与日志；不提交 Git
├─ preview.html             # 独立模拟交互预览
├─ setup-windows.ps1
├─ start-backend.ps1
└─ start-frontend.ps1
```

| 代码位置 | 负责的内容 |
|---|---|
| [`models.py`](../backend/app/models.py) | 输入输出字段、偏好默认值和参数上限 |
| [`main.py`](../backend/app/main.py)、[`production.py`](../backend/app/production.py) | API、请求生命周期、地图代理、静态文件及生产入口限流 |
| [`amap.py`](../backend/app/amap.py) | 地图请求、路线解析、自动推荐编排、正式核验与对照方案 |
| [`hub_discovery.py`](../backend/app/hub_discovery.py)、[`hub_store.py`](../backend/app/hub_store.py) | 枢纽召回、接近道路生成、SQLite 空间候选池 |
| [`coarse_routing.py`](../backend/app/coarse_routing.py)、[`osrm.py`](../backend/app/osrm.py)、[`coordinates.py`](../backend/app/coordinates.py) | 几何/矩阵粗排、OSRM 请求、坐标转换 |
| [`candidates.py`](../backend/app/candidates.py)、[`geometry.py`](../backend/app/geometry.py) | 普通道路候选、道路条件判断、距离与轨迹投影 |
| [`exit_discovery.py`](../backend/app/exit_discovery.py)、[`highway.py`](../backend/app/highway.py) | 出口发现、共同前缀/分流采样、出口途经顺序核验 |
| [`preselection.py`](../backend/app/preselection.py) | 候选去重、覆盖名额、预选诊断 |
| [`transit.py`](../backend/app/transit.py)、[`optimizer.py`](../backend/app/optimizer.py) | 公交步骤解析、交通方式约束、评分和最终排名 |
| [`manual.py`](../backend/app/manual.py)、[`demo.py`](../backend/app/demo.py) | 指定 P 的测算、明确标注的虚构演示数据 |
| [`map_cache.py`](../backend/app/map_cache.py)、[`search_log.py`](../backend/app/search_log.py)、[`search_replay.py`](../backend/app/search_replay.py) | 地图缓存、日志记录、按搜索编号读取回放 |
| [`MapCanvas.vue`](../frontend/src/components/MapCanvas.vue) | 仅高德底图、实时候选和核验路线、P 与公交首站 S、失败重试 |
| [`DriverItinerary.vue`](../frontend/src/components/DriverItinerary.vue)、[`TransitItinerary.vue`](../frontend/src/components/TransitItinerary.vue) | 司机原路线/经停对比、公共交通乘坐步骤 |
| [`ManualDropoffPanel.vue`](../frontend/src/components/ManualDropoffPanel.vue)、[`CandidateReplay.vue`](../frontend/src/components/CandidateReplay.vue) | 手动测试和候选流程回放 |

理解算法可按 `models.py → amap.py 的 calculate_live → hub_discovery.py → coarse_routing.py → optimizer.py` 阅读；排查未推荐原因时再查看预选、出口和回放模块。

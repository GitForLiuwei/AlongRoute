from datetime import datetime, timezone, timedelta

from .geometry import haversine_m, interpolate
from .models import (DrivingStep, ModeOption, Place, Point, Recommendation,
                     RecommendationRequest, RecommendationResponse, Route, TransitStep)
from .optimizer import rank_recommendations


def demo_suggestions(query: str):
    from .models import PlaceSuggestion
    data = [
        ("广州塔（示例）", "广州市海珠区", 113.3245, 23.1065, "020"),
        ("广州东站（示例）", "广州市天河区", 113.3279, 23.1370, "020"),
        ("珠江新城（示例）", "广州市天河区", 113.3210, 23.1195, "020"),
        ("天河客运站（示例）", "广州市天河区", 113.3437, 23.1718, "020"),
        ("广州南站（示例）", "广州市番禺区", 113.2690, 22.9890, "020"),
    ]
    return [PlaceSuggestion(name=n, address=a, point=Point(lon=x, lat=y), citycode=c)
            for n, a, x, y, c in data if not query or query.lower() in n.lower()][:8]


def create_demo(request: RecommendationRequest) -> RecommendationResponse:
    a = request.origin.point
    b = request.driver_destination.point
    c = request.passenger_destination.point
    dist = haversine_m(a, b)
    duration = max(360, round(dist / 9.5))
    baseline = Route(duration_s=duration, distance_m=round(dist * 1.3),
                     polyline=[a, interpolate(a, b, .34), interpolate(a, b, .7), b],
                     driving_steps=[DrivingStep(road_name="演示直达道路（虚构）", instruction="演示：从共同出发地前往司机终点",
                         distance_m=round(dist * 1.3), duration_s=duration)])
    specs = [(.30, 0, 25, 40, 45, 5, 750),
             (.56, 3, 15, 25, 22, 4, 350),
             (.80, 5, 12, 20, 18, 6, 260)]
    recs = []
    for i, (ratio, detour, taxi_min, taxi_fee, transit_min, transit_fee, walk) in enumerate(specs, 1):
        detour_s = max(90, detour * 60)
        if detour_s > request.preferences.max_detour_minutes * 60:
            continue
        p = interpolate(a, b, ratio)
        place = Place(name=f"演示候选点 P{i}（虚构）", point=p,
                      citycode=request.origin.citycode)
        shared_ride = round(duration * ratio)
        options = []
        if "taxi" in request.preferences.modes:
            options.append(ModeOption(
                mode="taxi", duration_s=taxi_min * 60,
                fare_yuan=taxi_fee, walking_m=100, transfers=0,
                details="模拟驾车时间；不含实际等车时间",
                route=[p, interpolate(p, c, .6), c],
                notes=["费用、耗时均为演示数据，不是实际报价"]))
        if "transit" in request.preferences.modes and walk <= request.preferences.max_walking_m and request.preferences.max_transfers >= 1:
            options.append(ModeOption(
                mode="transit", duration_s=transit_min * 60,
                fare_yuan=transit_fee, walking_m=walk, transfers=1,
                details="模拟公交/地铁换乘", route=[p, c],
                notes=["票价、时刻均为演示数据"] ))
            options[-1].boarding_stop = Place(name=f"演示换乘站{i}入口（虚构）",
                                             point=interpolate(p, c, .01), citycode=request.origin.citycode)
            options[-1].boarding_walk_m = (180, 220, 260)[i - 1]
            options[-1].boarding_kind = "subway"
            options[-1].boarding_walk_route = [p, options[-1].boarding_stop.point]
            access = options[-1].boarding_walk_m
            transfer = min(60, max(0, walk - access))
            final_walk = max(0, walk - access - transfer)
            walk_s = round(walk / 1.2)
            ride_s = max(0, transit_min * 60 - walk_s)
            options[-1].transit_steps = [
                TransitStep(kind="walk", title="步行到站", from_stop="下车点",
                    to_stop=options[-1].boarding_stop.name, distance_m=access, duration_s=round(access / 1.2),
                    instructions=["演示：沿步行路线前往虚构入口，不可用于真实导航"]),
                TransitStep(kind="subway", title="乘车", line_name="演示线路1（虚构）", direction="演示终点甲",
                    from_stop=f"演示换乘站{i}", to_stop="演示中转站", entrance="演示入口", stop_count=3,
                    via_stops=["演示途经站甲", "演示途经站乙"], duration_s=ride_s // 2),
                TransitStep(kind="walk", title="步行换乘", from_stop="演示中转站", to_stop="演示线路2站台",
                    distance_m=transfer, duration_s=round(transfer / 1.2)),
                TransitStep(kind="subway", title="换乘", line_name="演示线路2（虚构）", direction="演示终点乙",
                    from_stop="演示中转站", to_stop="演示到达站", stop_count=2, via_stops=["演示途经站丙"],
                    exit="演示出口", duration_s=ride_s - ride_s // 2),
                TransitStep(kind="walk", title="步行到目的地", from_stop="演示到达站", to_stop=request.passenger_destination.name,
                    distance_m=final_walk, duration_s=round(final_walk / 1.2))]
        if not options:
            continue
        for option in options:
            option.total_duration_s = shared_ride + 90 + option.duration_s
            option.arrival_at = (request.departure_time or datetime.now(timezone.utc)) + timedelta(seconds=option.total_duration_s)
        driver_route = Route(duration_s=duration + detour_s - 90,
                             distance_m=baseline.distance_m + detour * 100,
                             polyline=[a, p, b])
        first_distance = round(driver_route.distance_m * ratio)
        driver_route.driving_steps = [
            DrivingStep(road_name="演示前段道路（虚构）", instruction="演示：前往乘客下车点",
                distance_m=first_distance, duration_s=shared_ride),
            DrivingStep(road_name="演示后段道路（虚构）", instruction="演示：从下车点继续前往司机终点",
                distance_m=driver_route.distance_m - first_distance, duration_s=driver_route.duration_s - shared_ride)]
        recs.append(Recommendation(
            id=f"demo-p{i}", point=place, driver_detour_s=detour_s,
            shared_ride_s=shared_ride, stop_duration_s=90,
            candidate_source=("roadside", "station_area", "parking")[i - 1],
            driver_route=driver_route, options=options,
            suggested_mode=options[0].mode, score=0,
            reason="模拟候选；仅供验证产品交互，不能用于实际下车。"))
    recs = rank_recommendations(recs, request.preferences)
    return RecommendationResponse(
        mode="demo", baseline_route=baseline, recommendations=recs,
        warnings=["演示模式：路线、候选点、耗时、费用均为模拟，禁止用于真实停车或导航。",
                  "接入高德 Key 后才会请求真实路线；真实 POI 也不能自动视为合法停车位。"],
        generated_at=datetime.now(timezone.utc), examined_candidates=len(specs))

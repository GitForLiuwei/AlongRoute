"""Calculate a user's chosen drop-off without automatic discovery or filtering."""
from datetime import datetime, timedelta, timezone
import math
import os

import httpx

from .amap import AmapClient, LOCAL_TZ, MapServiceError
from .candidates import terminal_is_usable
from .models import ManualDropoffRequest, ManualDropoffResponse, ModeOption, Route
from .optimizer import taxi_score_seconds


async def calculate_manual(request: ManualDropoffRequest) -> ManualDropoffResponse:
    departure = request.departure_time or datetime.now(LOCAL_TZ)
    if departure.tzinfo is None:
        departure = departure.replace(tzinfo=LOCAL_TZ)
    if departure < datetime.now(LOCAL_TZ) - timedelta(minutes=2):
        raise MapServiceError("出发时间已经过去，请重新选择中国时间的出发时间")
    warnings = ["手动测试仅测算指定地点，不执行自动候选筛选；超出限制仍展示结果。",
                "停靠按90秒、接车缓冲按5分钟估算；出租车估价不是网约车实时报价。",
                "复用24小时内的地图缓存，时间与费用为估算，停靠条件仍待核实。"]
    async with httpx.AsyncClient() as client:
        api = AmapClient(os.getenv("AMAP_WEB_SERVICE_KEY", "").strip(), client)
        baseline = await api.driving(request.origin.point, request.driver_destination.point)
        if baseline is None:
            raise MapServiceError("无法获取司机原路线，请检查起点和司机目的地")
        baseline = min(list(api.driving_choices) or [baseline], key=lambda route: route.duration_s)
        driver = await api.driving(request.origin.point, request.driver_destination.point, via=request.dropoff.point)
        if driver is None or driver.dropoff_duration_s is None:
            raise MapServiceError("无法取得经过指定下车点的连续路线和到点耗时，请选择可驾车到达的道路或入口")
        driver = min((route for route in (list(api.driving_choices) or [driver])
                      if route.dropoff_duration_s is not None), key=lambda route: route.duration_s)
        road_usable = terminal_is_usable(Route(duration_s=driver.duration_s, distance_m=driver.distance_m,
            polyline=[request.dropoff.point], segments=driver.segments), request.dropoff.point, True)
        if not road_usable:
            warnings.append("指定下车点的道路条件未通过自动核验，仍展示测算结果；请另行确认实际上下客位置。")
        taxi = None
        try:
            taxi = await api.driving(request.dropoff.point, request.passenger_destination.point)
            if taxi is not None:
                taxi = min(list(api.driving_choices) or [taxi], key=lambda route: route.duration_s)
        except MapServiceError:
            warnings.append("下车后的打车路线查询失败，已保留司机测算结果；乘客全程耗时及费用暂无法确定。")
        if taxi is None:
            warnings.append("未取得下车后的打车路线，乘客全程耗时和打车费用显示为未知。")

    stop_s, wait_s = 90, 300
    driving_extra = max(0, driver.duration_s - baseline.duration_s)
    detour = driving_extra + stop_s
    shared = driver.dropoff_duration_s
    quoted_fare = taxi.fare_yuan if taxi is not None else None
    fare = quoted_fare if quoted_fare is not None and math.isfinite(quoted_fare) and quoted_fare >= 0 else None
    passenger_total = shared + stop_s + wait_s + taxi.duration_s if taxi is not None else None
    score = None
    if fare is not None and passenger_total is not None:
        option = ModeOption(mode="taxi", duration_s=wait_s + taxi.duration_s,
                            total_duration_s=passenger_total, fare_yuan=fare, details="指定下车点测算")
        score = float(taxi_score_seconds(detour, option, request.preferences.priority) / 60)
    generated = datetime.now(timezone.utc)
    return ManualDropoffResponse(
        origin=request.origin, driver_destination=request.driver_destination,
        passenger_destination=request.passenger_destination, dropoff=request.dropoff,
        baseline_route=baseline, driver_route=driver, taxi_route=taxi,
        driving_extra_s=driving_extra, driver_detour_s=detour, shared_ride_s=shared,
        stop_duration_s=stop_s, wait_estimate_s=wait_s, fare_yuan=fare,
        passenger_total_s=passenger_total, combined_score=score,
        arrival_at=departure + timedelta(seconds=passenger_total) if passenger_total is not None else None,
        max_detour_minutes=request.preferences.max_detour_minutes,
        max_taxi_fare_yuan=request.preferences.max_taxi_fare_yuan,
        within_detour_limit=detour <= request.preferences.max_detour_minutes * 60,
        within_taxi_fare_limit=fare <= request.preferences.max_taxi_fare_yuan if fare is not None else None,
        road_condition_usable=road_usable, warnings=warnings,
        generated_at=generated, departure_at=departure, expires_at=generated + timedelta(minutes=5))

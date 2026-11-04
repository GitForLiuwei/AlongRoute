"""AMap Web Service adapter. All actual credentials stay server-side."""
import asyncio
import math
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from weakref import WeakKeyDictionary

import httpx

from .geometry import haversine_m, project_to_path, point_along_path
from .models import (ModeOption, Place, PlaceSuggestion, Point, Preferences, Recommendation,
                     RecommendationRequest, RecommendationResponse, RoadSegment, Route, ComparisonOption)
from .optimizer import MAX_BOARDING_WALK_M, is_viable, rank_recommendations, option_score, preferred_option
from .candidates import Candidate, Station, build_candidates, search_centers, terminal_is_usable, controlled_segment
from .highway import ExitAnchor, highway_centers, original_exits, discovered_exit, exit_was_used
from .transit import coordinates, parse_itinerary

AMAP = "https://restapi.amap.com"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")


class MapServiceError(Exception):
    pass


class RequestGate:
    """Space request starts instead of sending a concurrent burst to AMap."""

    def __init__(self, interval=0.55):
        self.interval = interval
        self.lock = asyncio.Lock()
        self.next_start = 0.0

    async def wait(self):
        loop = asyncio.get_running_loop()
        async with self.lock:
            while (delay := self.next_start - loop.time()) > 0:
                await asyncio.sleep(delay)
            self.next_start = loop.time() + self.interval


# Shared by clients in the same server loop: overlapping searches also count.
_request_gates = WeakKeyDictionary()


async def wait_for_request_slot(path):
    loop = asyncio.get_running_loop()
    gates = _request_gates.setdefault(loop, {})
    await gates.setdefault(path, RequestGate()).wait()


def number(value, fallback=None):
    try:
        if value is None or value == "":
            return fallback
        parsed = float(value)
        return parsed if math.isfinite(parsed) else fallback
    except (ValueError, TypeError):
        return fallback


def polyline(value: str | None) -> list[Point]:
    return coordinates(value)


class AmapClient:
    def __init__(self, key: str, client: httpx.AsyncClient):
        self.key = key
        self.client = client
        self._semaphore = asyncio.Semaphore(4)
        self._requests = {}
        self._request_count = 0
        self.driving_choices = []

    async def get(self, path: str, **params):
        cache_key = (path, tuple(sorted((key, str(value)) for key, value in params.items())))
        cached = self._requests.get(cache_key)
        if cached is not None and cached.done() and cached.cancelled():
            del self._requests[cache_key]
        if cache_key not in self._requests:
            if self._request_count >= 250:
                raise MapServiceError("本次规划已达到250次接口预算，请缩小路线范围后重试")
            self._request_count += 1
            self._requests[cache_key] = asyncio.create_task(self._get(path, **params))
        return await self._requests[cache_key]

    async def _get(self, path: str, **params):
        async with self._semaphore:
            for attempt in range(3):
                await wait_for_request_slot(path)
                try:
                    response = await self.client.get(
                        f"{AMAP}{path}", params={"key": self.key, **params}, timeout=18)
                    response.raise_for_status()
                    data = response.json()
                except (httpx.HTTPError, ValueError) as exc:
                    raise MapServiceError("高德地图服务暂不可用，请稍后重试") from exc
                if str(data.get("status")) == "1":
                    return data
                rate_limited = (str(data.get("infocode")) in {
                    "10014", "10019", "10020", "10021", "10022", "10023"
                } or "QPS" in str(data.get("info", "")))
                if rate_limited and attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                    continue
                # Never include the key in logged/error text.
                if rate_limited:
                    raise MapServiceError("高德请求频率受限，重试后仍失败，请稍后再试")
                raise MapServiceError(f"地图接口错误：{data.get('info', '未知错误')}")

    async def driving(self, origin: Point, destination: Point, via: Point | list[Point] | None = None) -> Route | None:
        args = {"origin": origin.csv(), "destination": destination.csv(),
                "strategy": 32, "show_fields": "cost,polyline,navi"}
        if via:
            args["waypoints"] = ";".join(point.csv() for point in via) if isinstance(via, list) else via.csv()
        result = await self.get("/v5/direction/driving", **args)
        route_data = result.get("route") or {}
        paths = route_data.get("paths") or []
        if not paths:
            return None
        choices = [self.parse_driving(route_data, path, via[-1] if isinstance(via, list) else via) for path in paths[:3]]
        self.driving_choices = [route for route in choices if route is not None]
        return self.driving_choices[0] if self.driving_choices else None

    @staticmethod
    def parse_driving(route_data, path, via=None):
        coords, segments = [], []
        for step in path.get("steps") or []:
            points = polyline(step.get("polyline"))
            coords.extend(points)
            segments.append(RoadSegment(name=str(step.get("road_name") or ""), polyline=points,
                                        end_action=str((step.get("navi") or {}).get("action") or ""),
                                        duration_s=round(number((step.get("cost") or {}).get("duration"))) if number((step.get("cost") or {}).get("duration")) is not None else None,
                                        distance_m=round(number(step.get("step_distance", step.get("distance")), 0)),
                                        instruction=str(step.get("instruction") or ""),
                                        assistant_action=str((step.get("navi") or {}).get("assistant_action") or "")))
        duration = number((path.get("cost") or {}).get("duration"))
        if duration is None:
            # Do not guess route duration when the provider omitted it.
            return None
        fare = number(route_data.get("taxi_cost"))
        dropoff_duration = None
        if via:
            # One provider route, with one continuous geometry and timeline.
            elapsed = 0
            matches = []
            for segment in segments:
                if segment.duration_s is None:
                    return None
                if len(segment.polyline) < 2:
                    elapsed += segment.duration_s
                    continue
                _, progress, gap = project_to_path(via, segment.polyline)
                length = sum(haversine_m(a, b) for a, b in zip(segment.polyline, segment.polyline[1:]))
                if gap <= 5:
                    matches.append((gap, elapsed + segment.duration_s * progress / max(length, 1)))
                elapsed += segment.duration_s
            if not matches or any(haversine_m(a.polyline[-1], b.polyline[0]) > 5
                                  for a, b in zip(segments, segments[1:]) if a.polyline and b.polyline and min(haversine_m(via, a.polyline[-1]), haversine_m(via, b.polyline[0])) <= 50):
                return None
            dropoff_duration = min(round(duration), round(min(matches, key=lambda item: item[0])[1]))
        return Route(duration_s=round(duration), dropoff_duration_s=dropoff_duration,
                     distance_m=round(number(path.get("distance"), 0)),
                     polyline=coords, fare_yuan=fare, segments=segments, is_schematic=not bool(coords))

    async def walking(self, origin: Point, destination: Point) -> Route | None:
        response = await self.get("/v5/direction/walking", origin=origin.csv(),
                                  destination=destination.csv(), show_fields="cost,polyline")
        paths = (response.get("route") or {}).get("paths") or []
        routes = []
        for path in paths:
            distance = number(path.get("distance"))
            duration = number((path.get("cost") or {}).get("duration"))
            points = [point for step in path.get("steps") or [] for point in polyline(step.get("polyline"))]
            if distance is None or distance < 0 or duration is None or duration < 0 or not points:
                continue
            start_gap, end_gap = haversine_m(origin, points[0]), haversine_m(destination, points[-1])
            if start_gap > 5 or end_gap > 5:
                continue  # A remote map snap does not prove a walkable connection.
            routes.append(Route(duration_s=math.ceil(duration + (start_gap + end_gap) / 1.2), distance_m=math.ceil(distance + start_gap + end_gap),
                                polyline=points,
                                walk_instructions=[step["instruction"] for step in path.get("steps") or []
                                                   if isinstance(step.get("instruction"), str) and step["instruction"].strip()]))
        return min(routes, key=lambda route: route.distance_m) if routes else None

    async def nearby(self, center: Point, keyword: str) -> list[Place]:
        result = await self.get("/v5/place/around", location=center.csv(),
                                keywords=keyword, radius=1500 if keyword == "高速出口" else 800, page_size=25, show_fields="children")
        found = []
        pois = result.get("pois") or []
        pois = [item for poi in pois for item in [poi, *({**poi, **child, "children": []} for child in (poi.get("children") or []))]]
        for poi in pois:
            coords = polyline(poi.get("location"))
            if not coords:
                continue
            found.append(Place(
                name=poi.get("name") or "未命名候选场所", point=coords[0],
                citycode=str(poi.get("citycode") or "") or None,
                poi_id=str(poi.get("id") or "") or None))
        return found

    async def suggest(self, keyword: str) -> list[PlaceSuggestion]:
        # Input tips omit citycode; POI search includes it for transit planning.
        result = await self.get("/v5/place/text", keywords=keyword, page_size=8)
        output = []
        for item in result.get("pois") or []:
            coords = polyline(item.get("location"))
            if not coords:
                continue
            citycode = item.get("citycode")
            output.append(PlaceSuggestion(
                name=str(item.get("name") or ""), address=str(item.get("address") or ""),
                point=coords[0], citycode=str(citycode) if citycode and isinstance(citycode, (int, str)) else None,
                poi_id=str(item.get("id") or "") or None))
        return output[:8]

    async def transit(self, origin: Place, destination: Place, boarding_at: datetime,
                      prefs: Preferences) -> list[ModeOption]:
        if not origin.citycode or not destination.citycode:
            return []
        local = boarding_at.astimezone(LOCAL_TZ)
        response = await self.get(
            "/v5/direction/transit/integrated", origin=origin.point.csv(),
            destination=destination.point.csv(), city1=origin.citycode,
            city2=destination.citycode, strategy=0, AlternativeRoute=5,
            date=local.strftime("%Y-%m-%d"), time=local.strftime("%H-%M"),
            show_fields="cost,polyline")
        options, errors = [], []
        for item in (response.get("route") or {}).get("transits") or []:
            option = parse_itinerary(item, origin.citycode, destination.name)
            if option is None or option.transfers > prefs.max_transfers:
                continue
            if haversine_m(origin.point, option.boarding_stop.point) > MAX_BOARDING_WALK_M:
                continue
            # Verify the entrance/platform actually used by THIS itinerary.
            try:
                access = await self.walking(origin.point, option.boarding_stop.point)
            except MapServiceError as exc:
                errors.append(exc)
                continue
            if access is None or access.distance_m > MAX_BOARDING_WALK_M:
                continue
            option.boarding_walk_m = access.distance_m
            option.boarding_walk_route = access.polyline
            if option.original_access_m is None or option.original_access_s is None:
                continue  # Cannot reconcile a separately checked path without its original leg.
            delta_s = access.duration_s - option.original_access_s
            if delta_s > 30:
                # A materially slower replacement may miss the provider's service.
                # Reject rather than reuse a now-unreachable timetable.
                continue
            option.walking_m = option.walking_m - option.original_access_m + access.distance_m
            if option.walking_m < access.distance_m:
                continue
            option.duration_s += max(0, delta_s)
            if option.transit_steps and option.transit_steps[0].kind == "walk":
                first_walk = option.transit_steps[0]
                first_walk.distance_m = access.distance_m
                first_walk.duration_s = access.duration_s
                first_walk.to_stop = option.boarding_stop.name
                # The verified path replaces the provider's original access path.
                # Never show directions belonging to the superseded path.
                first_walk.instructions = access.walk_instructions
            option.notes.append("接驳步行已分段核算；班次、入口开放及站内通行仍需出发前确认")
            # Private cars may approach a bus stop, but must not occupy the platform.
            if option.boarding_kind == "bus" and haversine_m(origin.point, option.boarding_stop.point) <= 30:
                continue
            if is_viable(option, prefs):
                options.append(option)
        if not options and errors:
            raise errors[0]
        return options


async def calculate_live(request: RecommendationRequest) -> RecommendationResponse:
    from datetime import timezone
    key = os.getenv("AMAP_WEB_SERVICE_KEY", "").strip()
    now = datetime.now(LOCAL_TZ)
    departure = request.departure_time or now
    if departure.tzinfo is None:
        departure = departure.replace(tzinfo=LOCAL_TZ)
    if departure < now - timedelta(minutes=2):
        raise MapServiceError("出发时间已经过去，请重新选择中国时间的出发时间")
    warnings = ["当前为有预算的候选搜索，结果不保证全局最优。", "停靠位置的道路侧、禁停标志和靠边条件尚未核实；公交站是接驳目标，不代表私家车可占用站台。",
                "仅公共交通方案要求到实际站台或入口步行不超过300米；同等条件优先更近站点，打车不受此限制。",
                "驾车按查询时路况估算；到达时间不保证赶上具体班次，入口开放、安检及站内通行需另行确认。",
                "当前用于出发前规划，不跟踪车辆位置；选定方案后及出发前请重新计算。"]
    async with httpx.AsyncClient() as client:
        api = AmapClient(key, client)
        baseline = await api.driving(request.origin.point, request.driver_destination.point)
        if baseline is None or not baseline.polyline:
            raise MapServiceError("无法获取完整的司机原始路线")
        baselines = list(api.driving_choices) or [baseline]
        baseline = min(baselines, key=lambda route: route.duration_s)
        # Nearby alternatives receive a share of the discovery budget.
        baselines = [route for route in baselines if route.duration_s <= baseline.duration_s + 300]
        centers = []
        anchors = []
        for route in baselines:
            anchors.extend(original_exits(route, request.origin.citycode))
            for point in search_centers(route):
                if not any(haversine_m(point, existing) < 500 for existing in centers):
                    centers.append(point)
        # Bus-stop stopping checks are independent of the onward transport mode.
        kinds = ["停车场", "公交站"] + (["地铁站"] if "transit" in request.preferences.modes else [])
        search_errors, parking, stations = [], {}, {}
        complete = True
        searches_started = 0

        async def discover_one(point, kind):
            nonlocal searches_started
            searches_started += 1
            try:
                places = await api.nearby(point, kind)
                for place in places:
                    identity = place.poi_id or place.point.csv()
                    if kind == "高速出口":
                        for route in baselines:
                            anchor = discovered_exit(route, place)
                            if anchor and not any(haversine_m(anchor.place.point, old.place.point) < 100 for old in anchors):
                                anchors.append(anchor)
                    elif kind == "停车场":
                        parking[identity] = place
                    else:
                        stations[identity] = Station(place, "bus" if kind == "公交站" else "subway")
            except MapServiceError as exc:
                search_errors.append(exc)

        async def discover():
            motorway_centers = []
            for route in baselines:
                for point in highway_centers(route):
                    if not any(haversine_m(point, old) < 800 for old in motorway_centers):
                        motorway_centers.append(point)
            if motorway_centers:
                try:
                    await asyncio.wait_for(asyncio.gather(*(discover_one(point, "高速出口") for point in (motorway_centers if len(motorway_centers) <= 20 else [motorway_centers[round(index * (len(motorway_centers) - 1) / 19)] for index in range(20)]))), timeout=12)
                except asyncio.TimeoutError:
                    warnings.append("高速出口发现达到预算，部分出口未搜索。")
            anchors.sort(key=lambda anchor: (not anchor.original, haversine_m(anchor.place.point, request.passenger_destination.point)))
            del anchors[8:]
            # Exit anchors receive priority; do not search highway mainline for drop-offs.
            local_centers = [anchor.place.point for anchor in anchors]
            for point in centers:
                if not any(haversine_m(point, old) < 500 for old in local_centers):
                    local_centers.append(point)
            await asyncio.gather(*(discover_one(point, kind) for point in local_centers[:40] for kind in kinds))
        try:
            await asyncio.wait_for(discover(), timeout=30)
        except asyncio.TimeoutError:
            complete = False
            warnings.append("沿途搜索达到30秒预算，搜索覆盖不完整。")
        if search_errors and len(search_errors) == searches_started:
            raise MapServiceError("沿途地点查询失败，请稍后重试")
        if search_errors:
            complete = False
            warnings.append("部分沿途地点查询失败，本次搜索范围不完整。")
        if any(highway_centers(route) for route in baselines):
            warnings.append("高速路段仅围绕出口外寻找候选；出口可达性按连续途经路线核验，增加时间包含驶出、后续前往终点及停靠。")
        if baseline.distance_m > 70000:
            complete = False
            warnings.append("长路线搜索已达到中心数量上限，部分沿线区域可能未覆盖。")
        candidates = []
        # Keep separate discovery pools so transit proximity cannot crowd out taxi options.
        pools = {mode: [] for mode in request.preferences.modes}
        for mode, pool in pools.items():
            single = request.model_copy(update={"preferences": request.preferences.model_copy(update={"modes": [mode]})})
            for route in baselines:
                for candidate in build_candidates(route, list(parking.values()), list(stations.values()), single):
                    candidate.modes = (mode,)
                    pool.append(candidate)

        # Generate actual car approaches to stations, rather than projecting every
        # station onto the driver's original road, which may be a highway.
        if "transit" in pools:
            near_stations = sorted(stations.values(), key=lambda station:
                min(project_to_path(station.place.point, route.polyline)[2] for route in baselines))[:8]
            async def station_approaches(station):
                try:
                    route = await api.driving(request.origin.point, station.place.point)
                    if route is None or len(route.polyline) < 2:
                        return []
                    length = sum(haversine_m(a, b) for a, b in zip(route.polyline, route.polyline[1:]))
                    found = []
                    offsets = (40, 70, 120) if station.kind == "bus" else (10, 30, 70)
                    for offset in offsets:
                        point = point_along_path(route.polyline, max(0, length - offset))
                        if station.kind == "bus" and haversine_m(point, station.place.point) <= 30:
                            continue
                        if not terminal_is_usable(Route(duration_s=route.duration_s, distance_m=route.distance_m,
                            polyline=[point], segments=route.segments), point, True):
                            continue
                        _, progress, lateral = project_to_path(point, baseline.polyline)
                        found.append(Candidate(Place(name=f"{station.place.name}附近下客候选"[:120], point=point,
                            citycode=station.place.citycode), "station_area", progress, offset,
                            route.duration_s, modes=("transit",)))
                    return found
                except MapServiceError:
                    return []
            try:
                approaches = await asyncio.wait_for(asyncio.gather(*(station_approaches(station) for station in near_stations)), timeout=10)
                pools["transit"] = [candidate for group in approaches for candidate in group] + pools["transit"]
            except asyncio.TimeoutError:
                complete = False
                warnings.append("部分近站下客位置未完成搜索，已保留其他候选。")

        def attach_exit(candidate):
            closest = min(((project_to_path(candidate.place.point, segment.polyline)[2], segment)
                for route in baselines for segment in route.segments if segment.polyline), default=None, key=lambda item: item[0])
            eligible = [anchor for anchor in anchors if haversine_m(candidate.place.point, anchor.place.point) <= 2500
                        and candidate.progress_m >= anchor.progress_m - 500]
            if (closest and controlled_segment(closest[1])) or eligible:
                if not eligible:
                    return False
                anchor = min(eligible, key=lambda value: (not value.original, haversine_m(candidate.place.point, value.place.point)))
                candidate.exit_place, candidate.approach, candidate.original_exit = anchor.place, anchor.approach, anchor.original
            return True

        # Interleave the independent pools. A point may support both modes, but
        # failure of its transit access never invalidates a valid taxi option.
        seen = {}
        for index in range(max((len(pool) for pool in pools.values()), default=0)):
            for mode, pool in pools.items():
                if index >= len(pool):
                    continue
                candidate = pool[index]
                key = candidate.place.point.csv()
                if key in seen:
                    existing = seen[key]
                    existing.modes = tuple(dict.fromkeys((*existing.modes, mode)))
                    continue
                if not attach_exit(candidate):
                    continue
                seen[key] = candidate
                candidates.append(candidate)
            if len(candidates) >= 48:
                break
        candidates = candidates[:48]
        errors, results = [], []
        evaluated_count = 0

        async def onward_options(place, at, modes=None):
            modes = request.preferences.modes if modes is None else modes
            options = []
            if "taxi" in modes:
                try:
                    taxi = await api.driving(place.point, request.passenger_destination.point)
                    if taxi:
                        wait = 300
                        options.append(ModeOption(mode="taxi", duration_s=taxi.duration_s + wait,
                            wait_estimate_s=wait, fare_yuan=taxi.fare_yuan, walking_m=None, transfers=0,
                            details="驾车时间＋假设5分钟接车缓冲（非实时等车预测）", route=taxi.polyline,
                            notes=["未接入车辆供给；接车位置待确认，实际等待可能更久", "出租车估价，非网约车实时报价"]))
                except MapServiceError as exc:
                    errors.append(exc)
            if "transit" in modes:
                try:
                    options.extend(await api.transit(place, request.passenger_destination, at, request.preferences))
                except MapServiceError as exc:
                    errors.append(exc)
            return options

        async def evaluate(candidate):
            nonlocal evaluated_count
            evaluated_count += 1
            place = candidate.place
            try:
                waypoints = [candidate.approach, candidate.exit_place.point, place.point] if candidate.exit_place and candidate.approach else place.point
                driver = await api.driving(request.origin.point, request.driver_destination.point, via=waypoints)
                if driver is None or driver.dropoff_duration_s is None:
                    return
                if candidate.exit_place and not exit_was_used(driver,
                    ExitAnchor(candidate.exit_place, candidate.approach, candidate.progress_m, candidate.original_exit), place.point):
                    return
                # Require known named road geometry near P; still not parking permission.
                if not terminal_is_usable(Route(duration_s=driver.duration_s, distance_m=driver.distance_m,
                    polyline=[place.point], segments=driver.segments), place.point, True):
                    return
                stop_s = 180 if candidate.source == "parking" else 90
                driving_extra = max(0, driver.duration_s - baseline.duration_s)
                extra = driving_extra + stop_s
                if extra > request.preferences.max_detour_minutes * 60:
                    return
                shared = driver.dropoff_duration_s
                options = await onward_options(place, departure + timedelta(seconds=shared + stop_s), request.preferences.modes)
                for option in options:
                    option.total_duration_s = shared + stop_s + option.duration_s
                    option.arrival_at = departure + timedelta(seconds=option.total_duration_s)
                if not options:
                    return
                # Keep the preferred transit alternative and taxi for a concise UI.
                transits = [o for o in options if o.mode == "transit"]
                options = [o for o in options if o.mode == "taxi"]
                if transits:
                    options.append(preferred_option(transits, request.preferences.priority))
                results.append(Recommendation(id=place.poi_id or f"p-{place.point.csv()}", point=place,
                    driver_detour_s=extra, driving_extra_s=driving_extra, driver_route=driver,
                    highway_exit=candidate.exit_place, original_exit=candidate.original_exit,
                    shared_ride_s=shared, stop_duration_s=stop_s, candidate_source=candidate.source,
                    options=options, suggested_mode=options[0].mode, score=0,
                    reason="连续途经路线估算；到P时间按途经路段内进度估算，停靠条件待核实"))
            except MapServiceError as exc:
                errors.append(exc)

        comparisons = []
        async def compare(label, place, shared):
            options = await onward_options(place, departure + timedelta(seconds=shared))
            for option in options:
                option.total_duration_s = shared + option.duration_s
                option.arrival_at = departure + timedelta(seconds=option.total_duration_s)
            if options:
                comparisons.append(ComparisonOption(label=label, options=options))

        async def plan():
            # Batch expansion: do not spend the entire quota when enough points already work.
            for offset in range(0, len(candidates), 8):
                await asyncio.gather(*(evaluate(c) for c in candidates[offset:offset + 8]))
                covered_modes = {option.mode for result in results for option in result.options}
                if len(results) >= 6 and set(request.preferences.modes) <= covered_modes:
                    break
            # Refine around the station actually used by successful itineraries.
            # This is separate from arbitrary nearby station POIs discovered earlier.
            if "transit" in pools:
                stops = {}
                for rec in sorted(results, key=lambda rec: min(option_score(o, request.preferences.priority) for o in rec.options)):
                    transit_options = [o for o in rec.options if o.mode == "transit"]
                    if transit_options:
                        option = preferred_option(transit_options, request.preferences.priority)
                        stops.setdefault(option.boarding_stop.point.csv(), Station(option.boarding_stop, option.boarding_kind))
                    if len(stops) >= 2:
                        break
                refined = await asyncio.gather(*(station_approaches(station) for station in stops.values()))
                next_candidates = []
                for group in refined:
                    for candidate in group:
                        key = candidate.place.point.csv()
                        if key not in seen and attach_exit(candidate):
                            seen[key] = candidate
                            next_candidates.append(candidate)
                candidates.extend(next_candidates)
                await asyncio.gather(*(evaluate(candidate) for candidate in next_candidates))
            comparisons_to_run = [compare("随车到B后前往C", request.driver_destination, baseline.duration_s + 90)]
            if terminal_is_usable(baseline, request.origin.point, False):
                comparisons_to_run.append(compare("从A直接前往C", request.origin, 0))
            else:
                warnings.append("起点道路条件不足以支持直接换乘，不计算从A立即换乘的对照方案。")
            await asyncio.gather(*comparisons_to_run)
        try:
            await asyncio.wait_for(plan(), timeout=60)
        except asyncio.TimeoutError:
            complete = False
            warnings.append("计算达到60秒预算，仅返回已完整验证的部分方案；对照方案可能未完成。")
        if errors:
            complete = False
            if not results and not comparisons:
                raise errors[0]
            warnings.append("部分交通方案查询失败，本次推荐不完整，请稍后重试。")
        ranked = rank_recommendations(results, request.preferences)
        for rec in ranked:
            chosen = preferred_option(rec.options, request.preferences.priority)
            alternatives = [(comparison.label, o) for comparison in comparisons for o in comparison.options]
            if alternatives:
                label, alternative = min(alternatives, key=lambda pair: option_score(pair[1], request.preferences.priority))
                delta = chosen.total_duration_s - alternative.total_duration_s
                rec.comparison = f"相较于{label}，预计{'多用' if delta > 0 else '节省'}{abs(delta) / 60:.1f}分钟；费用及步行请结合对照比较。"
                if option_score(alternative, request.preferences.priority) <= option_score(chosen, request.preferences.priority):
                    rec.comparison += " 按当前偏好，对照方案更优或相当，建议优先考虑。"
        if request.preferences.priority == "cost":
            warnings.append("省费用仅比较不比当前最快候选多耗时15分钟以上的方案；未知费用不视为低价。")
        if not comparisons:
            warnings.append("本次未取得符合接驳约束的对照方案，无法判断中途下车是否优于直接出行。")
        if evaluated_count < len(candidates):
            warnings.append("已保留足够可用候选，剩余候选尚未精算，推荐不代表全局最优。")
        if not ranked:
            warnings.append("已搜索范围内暂无符合限制的候选，不代表所有路线均无方案；公交接驳不会自动放宽300米限制。" if "transit" in request.preferences.modes else "已搜索范围内暂无满足司机增加时间的打车候选；打车不要求靠近公交或地铁。")
        generated = datetime.now(timezone.utc)
        return RecommendationResponse(mode="live", baseline_route=baseline, recommendations=ranked,
            comparisons=comparisons, warnings=warnings, generated_at=generated, departure_at=departure,
            expires_at=generated + timedelta(minutes=5), examined_candidates=evaluated_count, search_complete=complete and evaluated_count == len(candidates))

"""AMap Web Service adapter. All actual credentials stay server-side."""
import asyncio
from copy import deepcopy
import math
import os
from hashlib import sha256
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from weakref import WeakKeyDictionary

import httpx

from .geometry import haversine_m, project_to_path, point_along_path
from .models import (ModeOption, Place, PlaceSuggestion, Point, Preferences, Recommendation,
                     RecommendationRequest, RecommendationResponse, RoadSegment, Route, ComparisonOption, DrivingStep)
from .optimizer import MAX_BOARDING_WALK_M, is_viable, rank_recommendations, option_score, preferred_option, taxi_score_seconds
from .candidates import (Candidate, Station, build_candidates, search_centers, terminal_is_usable,
                         controlled_segment, restricted_road, dropoff_road_name, spread_items, interleave_groups)
from .highway import ExitAnchor, highway_centers, original_exits, discovered_exit, exit_was_used
from .exit_discovery import FocusedDiscovery, closest_location
from .transit import coordinates, parse_itinerary
from .preselection import candidate_key, candidate_trace
from .hub_discovery import HubDiscovery
from .search_log import event, recommendation_summary
from .map_cache import response_cache
from time import perf_counter

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
        started = perf_counter()
        shared_key = response_cache.key(self.key, path, params)
        shared = response_cache.get(shared_key)
        if shared is not None:
            event("map_request_finished", endpoint=path, status="ok", cache="hit",
                  request_count=self._request_count, duration_ms=round((perf_counter() - started) * 1000))
            return shared
        cache_key = (path, tuple(sorted((key, str(value)) for key, value in params.items())))
        cached = self._requests.get(cache_key)
        if cached is not None and cached.done() and cached.cancelled():
            del self._requests[cache_key]
        if cache_key not in self._requests:
            if self._request_count >= 250:
                raise MapServiceError("本次规划已达到250次接口预算，请缩小路线范围后重试")
            self._request_count += 1
            async def fetch():
                result = await self._get(path, **params)
                response_cache.put(shared_key, path, result)
                return result
            self._requests[cache_key] = asyncio.create_task(fetch())
        pending = self._requests[cache_key]
        try:
            result = await pending
            event("map_request_finished", endpoint=path, status="ok", cache="miss", request_count=self._request_count,
                  duration_ms=round((perf_counter() - started) * 1000))
            return deepcopy(result)
        except asyncio.CancelledError:
            event("map_request_finished", endpoint=path, status="cancelled")
            raise
        except Exception as exc:
            event("map_request_finished", endpoint=path, status="error", error_type=type(exc).__name__,
                  duration_ms=round((perf_counter() - started) * 1000))
            raise
        finally:
            if pending.done() and self._requests.get(cache_key) is pending:
                del self._requests[cache_key]

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
                                        distance_m=round(number(step.get("step_distance", step.get("distance")))) if number(step.get("step_distance", step.get("distance"))) is not None else None,
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
                     polyline=coords, fare_yuan=fare, segments=segments, is_schematic=not bool(coords),
                     driving_steps=[DrivingStep(road_name=s.name, instruction=s.instruction,
                         action=s.end_action, assistant_action=s.assistant_action,
                         distance_m=s.distance_m, duration_s=s.duration_s) for s in segments])

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

    async def nearby(self, center: Point, keyword: str, radius: int | None = None) -> list[Place]:
        result = await self.get("/v5/place/around", location=center.csv(),
                                keywords=keyword, radius=radius if radius is not None else (1500 if keyword == "高速出口" else 800),
                                page_size=25, show_fields="children")
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
            if is_viable(option, prefs):
                options.append(option)
        if not options and errors:
            raise errors[0]
        return options


async def calculate_live(request: RecommendationRequest) -> RecommendationResponse:
    from datetime import timezone
    planning_started = perf_counter()
    key = os.getenv("AMAP_WEB_SERVICE_KEY", "").strip()
    now = datetime.now(LOCAL_TZ)
    departure = request.departure_time or now
    if departure.tzinfo is None:
        departure = departure.replace(tzinfo=LOCAL_TZ)
    if departure < now - timedelta(minutes=2):
        raise MapServiceError("出发时间已经过去，请重新选择中国时间的出发时间")
    warnings = ["当前为有预算的候选搜索，结果不保证全局最优。", "公交站台附近允许进入候选筛选；停靠位置的道路侧、禁停标志和靠边条件尚未核实。",
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
        event("baseline_ready", duration_s=baseline.duration_s, distance_m=baseline.distance_m,
              alternatives=len(baselines))
        # Reserve ten minutes of the driver's allowance for reaching a drop-off and stopping.
        discovery_extra_s = max(0, request.preferences.max_detour_minutes - 10) * 60
        baselines = [route for route in baselines
                     if route.duration_s <= baseline.duration_s + discovery_extra_s]
        complete = True
        hub_first = os.getenv("CANDIDATE_STRATEGY", "hub_first").strip() != "legacy"
        focused_exits = any(controlled_segment(segment) for route in baselines for segment in route.segments)
        if hub_first:
            discovery = await HubDiscovery().run(api, request, baselines)
            candidates = discovery.candidates
            complete = discovery.complete
            warnings.extend(dict.fromkeys(discovery.warnings))
            anchors = [anchor for route in baselines for anchor in original_exits(route, request.origin.citycode)]
            pools = {mode: [] for mode in request.preferences.modes}
            event("candidates_ready", strategy="hub_first", count=len(candidates),
                  candidates=[dict(point=c.place.model_dump(mode="json"), **candidate_trace(c)) for c in candidates])
        elif focused_exits:
            discovery = await FocusedDiscovery().run(api, request, baselines)
            candidates = discovery.candidates
            complete = discovery.complete
            warnings.extend(dict.fromkeys(discovery.warnings))
            warnings.append("高速、快速路和高架按Q前后各最多三个出口搜索，在分流前后按近、中、远距离生成候选，并补充原路线普通道路；公交站点在各方向代表点周围5公里搜索。")
            warnings.append("同时沿最多两条A→C探索路线寻找靠近乘客目的地的出口，每条最多三个；两批候选统一核算完整A→P→B的司机增加时间及乘客后续费用。")
            warnings.append("出口前后的相对位置按司机行驶顺序计算，Q仅用于定位搜索区域；每个候选仍核验实际驶出及完整A→P→B路线。")
            if discovery.failed and not candidates and not discovery.succeeded:
                raise MapServiceError("出口及接驳地点查询失败，请稍后重试")
            pools = {mode: [] for mode in request.preferences.modes}
            event("candidates_ready", strategy="exit_split", count=len(candidates),
                  candidates=[dict(point=c.place.model_dump(mode="json"), source=c.source,
                                   modes=c.modes, discovery_group=c.discovery_group) for c in candidates])
        else:
            destination_routes = []
            try:
                passenger_route = await asyncio.wait_for(api.driving(
                    request.origin.point, request.passenger_destination.point), timeout=12)
                if passenger_route and passenger_route.polyline:
                    destination_routes = (list(api.driving_choices) or [passenger_route])[:2]
                else:
                    complete = False
                    warnings.append("未取得朝乘客目的地的探索路线，仍保留原路线搜索。")
            except (MapServiceError, asyncio.TimeoutError):
                complete = False
                warnings.append("朝乘客目的地的路线探索未完成，搜索覆盖不完整。")
            exploration_routes = baselines + destination_routes
            event("destination_corridors_ready", count=len(destination_routes),
                  routes=[dict(duration_s=route.duration_s, distance_m=route.distance_m)
                          for route in destination_routes])
            centers = []
            destination_centers = []
            anchors = []
            for route_index, route in enumerate(exploration_routes):
                is_destination = route_index >= len(baselines)
                for anchor in original_exits(route, request.passenger_destination.citycode if is_destination else request.origin.citycode):
                    anchor.original = not is_destination
                    if not any(haversine_m(anchor.place.point, old.place.point) < 100 for old in anchors):
                        anchors.append(anchor)
                target_centers = destination_centers if is_destination else centers
                for point in search_centers(route):
                    if not any(haversine_m(point, existing) < 500 for existing in target_centers):
                        target_centers.append(point)
            # Station locations support transit discovery, without a drop-off exclusion radius.
            kinds = ["停车场", "公交站"] + (["地铁站"] if "transit" in request.preferences.modes else [])
            search_errors, parking, stations = [], {}, {}
            searches_started = 0
            exit_area_centers = []

            async def discover_one(point, kind):
                nonlocal searches_started
                searches_started += 1
                try:
                    places = await api.nearby(point, kind)
                    for place in places:
                        identity = place.poi_id or place.point.csv()
                        if kind == "高速出口":
                            for route in exploration_routes:
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
                passenger_motorway_centers = []
                for route in destination_routes:
                    for point in highway_centers(route):
                        if not any(haversine_m(point, old) < 800 for old in passenger_motorway_centers):
                            passenger_motorway_centers.append(point)
                focused = sorted(passenger_motorway_centers,
                                 key=lambda point: haversine_m(point, request.passenger_destination.point))[:8]
                exit_area_centers[:] = focused
                highway_queries = []
                for point in interleave_groups([focused, spread_items(motorway_centers, 20 - len(focused))]):
                    if not any(haversine_m(point, old) < 500 for old in highway_queries):
                        highway_queries.append(point)
                if highway_queries:
                    try:
                        await asyncio.wait_for(asyncio.gather(*(discover_one(point, "高速出口") for point in highway_queries)), timeout=12)
                    except asyncio.TimeoutError:
                        warnings.append("高速出口发现达到预算，部分出口未搜索。")
                closest_anchors = sorted(anchors, key=lambda anchor:
                    haversine_m(anchor.place.point, request.passenger_destination.point))
                chosen = closest_anchors[:4]
                for anchor in sorted(anchors, key=lambda anchor: (not anchor.original,
                        haversine_m(anchor.place.point, request.passenger_destination.point))):
                    if anchor not in chosen and len(chosen) < 8:
                        chosen.append(anchor)
                anchors[:] = chosen
                # Reserve search centers near C and exit-outside roads before along-route coverage.
                local_centers = []
                groups = [[request.passenger_destination.point] if destination_routes else [],
                          exit_area_centers,
                          [anchor.place.point for anchor in anchors],
                          sorted(destination_centers, key=lambda point:
                                 haversine_m(point, request.passenger_destination.point))[:16],
                          spread_items(centers, 20)]
                for point in interleave_groups(groups):
                    if not any(haversine_m(point, old) < 500 for old in local_centers):
                        local_centers.append(point)
                    if len(local_centers) >= 40:
                        break
                event("discovery_coverage", destination_route_count=len(destination_routes),
                      highway_centers=[point.model_dump() for point in highway_queries],
                      local_centers=[point.model_dump() for point in local_centers],
                      exits=[anchor.place.model_dump(mode="json") for anchor in anchors])
                await asyncio.gather(*(discover_one(point, kind) for point in local_centers for kind in kinds))
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
            # Explore actual ordinary-road approaches to POIs outside the focused highway
            # areas, even when an exit POI is absent or not among the eight chosen anchors.
            regional_centers = []
            remaining_centers = list(exit_area_centers)
            if remaining_centers:
                regional_centers.append(remaining_centers.pop(0))
            while remaining_centers and len(regional_centers) < 4:
                point = max(remaining_centers, key=lambda q:
                            min(haversine_m(q, old) for old in regional_centers))
                remaining_centers.remove(point)
                if min(haversine_m(point, old) for old in regional_centers) > 800:
                    regional_centers.append(point)
            approach_targets = []
            for center in regional_centers:
                nearby = sorted((place for place in parking.values() if not restricted_road(place.name)
                                 and haversine_m(place.point, center) <= 1600),
                                key=lambda place: haversine_m(place.point, center))
                if nearby and not any(haversine_m(nearby[0].point, old.point) < 500 for old in approach_targets):
                    approach_targets.append(nearby[0])
            approach_route_ids = set()
            async def explore_approach(place):
                try:
                    route = await api.driving(request.origin.point, place.point)
                    return route if route and route.polyline else None
                except MapServiceError:
                    return None
            if approach_targets:
                try:
                    approaches = await asyncio.wait_for(asyncio.gather(
                        *(explore_approach(place) for place in approach_targets)), timeout=10)
                    for route in approaches:
                        if route is not None:
                            destination_routes.append(route)
                            approach_route_ids.add(id(route))
                    exploration_routes = baselines + destination_routes
                    event("exit_area_approaches", targets=[place.model_dump(mode="json") for place in approach_targets],
                          completed=len(approach_route_ids))
                except asyncio.TimeoutError:
                    complete = False
                    warnings.append("部分出口外道路探索达到时间预算，仍保留已取得的候选区域。")
            if any(highway_centers(route) for route in exploration_routes):
                warnings.append("高速路段仅围绕出口外寻找候选；出口可达性按连续途经路线核验，增加时间包含驶出、后续前往终点及停靠。")
            if baseline.distance_m > 70000:
                complete = False
                warnings.append("长路线已分配原路线、乘客方向和出口外道路的搜索名额，数量仍有限，部分区域可能未覆盖。")
            warnings.append("国道、省道的普通路段允许作为候选；高速、匝道及桥隧等受限路段仍排除，具体停靠条件待核实。")
            candidates = []
            # Keep separate discovery pools so transit proximity cannot crowd out taxi options.
            pools = {mode: [] for mode in request.preferences.modes}
            for mode, pool in pools.items():
                single = request.model_copy(update={"preferences": request.preferences.model_copy(update={"modes": [mode]})})
                route_groups = []
                for route_index, route in enumerate(exploration_routes):
                    is_destination = route_index >= len(baselines)
                    probes = spread_items(search_centers(route), 24) or spread_items(route.polyline, 24)
                    nearby_parking = sorted(parking.values(), key=lambda place:
                        min(haversine_m(place.point, point) for point in probes))[:80]
                    if is_destination:
                        nearby_parking = [place for place in nearby_parking if
                                          min(haversine_m(place.point, point) for point in probes) <= 1500]
                    route_request = single
                    if is_destination:
                        route_request = single.model_copy(update={"origin": single.origin.model_copy(
                            update={"citycode": request.passenger_destination.citycode or single.origin.citycode})})
                    sampling_route = route
                    if id(route) in approach_route_ids:
                        tail, length = [], 0
                        for segment in reversed(route.segments):
                            tail.append(segment)
                            length += sum(haversine_m(a, b) for a, b in zip(segment.polyline, segment.polyline[1:]))
                            if length >= 2000:
                                break
                        sampling_route = route.model_copy(update={"segments": list(reversed(tail))})
                    discovered = build_candidates(sampling_route, nearby_parking, list(stations.values()), route_request)
                    if is_destination:
                        discovered.sort(key=lambda candidate: (
                            haversine_m(candidate.place.point, request.passenger_destination.point), candidate.estimate_s))
                        discovered = interleave_groups([
                            [candidate for candidate in discovered if candidate.source == "roadside"],
                            [candidate for candidate in discovered if candidate.source == "parking"],
                            [candidate for candidate in discovered if candidate.source == "station_area"]])
                    event("candidate_pool_created", mode=mode, count=len(discovered),
                          discovery_group="destination" if is_destination else "baseline", route_index=route_index,
                          parking_count=len(nearby_parking), station_count=len(stations),
                          roadside_count=sum(candidate.source == "roadside" for candidate in discovered))
                    for candidate in discovered:
                        candidate.modes = (mode,)
                        candidate.discovery_group = "destination" if is_destination else "baseline"
                    route_groups.append(discovered)
                pool.extend(interleave_groups(route_groups))

            # Generate actual car approaches to stations, rather than projecting every
            # station onto the driver's original road, which may be a highway.
            if "transit" in pools:
                near_stations = []
                station_groups = [sorted(stations.values(), key=lambda station:
                    haversine_m(station.place.point, request.passenger_destination.point))[:4],
                    sorted(stations.values(), key=lambda station:
                        min(project_to_path(station.place.point, route.polyline)[2]
                            for route in exploration_routes))[:8]]
                for station in interleave_groups(station_groups):
                    if station not in near_stations:
                        near_stations.append(station)
                    if len(near_stations) >= 8:
                        break
                async def station_approaches(station):
                    try:
                        route = await api.driving(request.origin.point, station.place.point)
                        if route is None or len(route.polyline) < 2:
                            return []
                        length = sum(haversine_m(a, b) for a, b in zip(route.polyline, route.polyline[1:]))
                        found = []
                        offsets = (10, 30, 40, 70, 120) if station.kind == "bus" else (10, 30, 70)
                        for offset in offsets:
                            point = point_along_path(route.polyline, max(0, length - offset))
                            if not terminal_is_usable(Route(duration_s=route.duration_s, distance_m=route.distance_m,
                                polyline=[point], segments=route.segments), point, True):
                                continue
                            _, progress, lateral = project_to_path(point, baseline.polyline)
                            found.append(Candidate(Place(name=f"{station.place.name}附近下客候选"[:120], point=point,
                                citycode=station.place.citycode), "station_area", progress, offset,
                                route.duration_s, modes=("transit",), discovery_group="destination"))
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
                # These points came from an actual A->C road corridor. A continuous A->P->B
                # query will establish the exit actually used, without forcing a remote original exit.
                if candidate.discovery_group == "destination":
                    return True
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
                        if candidate.discovery_group == "destination":
                            existing.discovery_group = "destination"
                            existing.exit_place = existing.approach = None
                            existing.original_exit = False
                        continue
                    if not attach_exit(candidate):
                        continue
                    seen[key] = candidate
                    candidates.append(candidate)
                if len(candidates) >= 48:
                    break
            candidates = candidates[:48]
            event("candidates_ready", count=len(candidates),
                  candidates=[dict(point=c.place.model_dump(mode="json"), source=c.source, modes=c.modes,
                                   discovery_group=c.discovery_group) for c in candidates])
        errors, results = [], []
        evaluated_count = 0
        evaluated_regions = set()
        required_regions = {(c.route_index, c.region) for c in candidates if c.region}

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
            return [option for option in options
                    if option.mode != "taxi" or is_viable(option, request.preferences)]

        async def evaluate(candidate):
            nonlocal evaluated_count
            evaluated_count += 1
            evaluated_regions.add((candidate.route_index, candidate.region))
            place = candidate.place
            event("candidate_started", point=place.model_dump(mode="json"), **candidate_trace(candidate))
            def reject(reason, **fields):
                event("candidate_rejected", point=place.model_dump(mode="json"), reason=reason, **candidate_trace(candidate), **fields)
            try:
                waypoints = [candidate.approach, candidate.exit_place.point, place.point] if candidate.exit_place and candidate.approach else place.point
                driver = await api.driving(request.origin.point, request.driver_destination.point, via=waypoints)
                if driver is None or driver.dropoff_duration_s is None:
                    reject("missing_continuous_driver_route")
                    return
                if candidate.exit_place and not exit_was_used(driver,
                    ExitAnchor(candidate.exit_place, candidate.approach, candidate.progress_m, candidate.original_exit), place.point):
                    reject("exit_not_reached_in_order")
                    return
                # Require known named road geometry near P; still not parking permission.
                if not terminal_is_usable(Route(duration_s=driver.duration_s, distance_m=driver.distance_m,
                    polyline=[place.point], segments=driver.segments), place.point, True):
                    reject("unusable_or_unknown_road")
                    return
                used_exit_place = candidate.exit_place
                used_original_exit = candidate.original_exit
                if candidate.discovery_group in {"destination", "hub", "hub_surface"}:
                    _, dropoff_progress, _ = project_to_path(place.point, driver.polyline)
                    used_before_dropoff = [anchor for anchor in original_exits(driver, place.citycode)
                                           if anchor.progress_m < dropoff_progress]
                    if used_before_dropoff:
                        used = max(used_before_dropoff, key=lambda anchor: anchor.progress_m)
                        used_exit_place = used.place.model_copy(update={
                            "name": used.place.name.replace("原路线驶出后：", "实际驶出后：")})
                        used_original_exit = any(anchor.original and
                            haversine_m(anchor.place.point, used.place.point) < 100 for anchor in anchors)
                stop_s = 180 if candidate.source == "parking" else 90
                driving_extra = max(0, driver.duration_s - baseline.duration_s)
                extra = driving_extra + stop_s
                if extra > request.preferences.max_detour_minutes * 60:
                    reject("detour_limit", driver_detour_s=extra, limit_s=request.preferences.max_detour_minutes * 60)
                    return
                shared = driver.dropoff_duration_s
                options = await onward_options(place, departure + timedelta(seconds=shared + stop_s), request.preferences.modes)
                for option in options:
                    option.total_duration_s = shared + stop_s + option.duration_s
                    option.arrival_at = departure + timedelta(seconds=option.total_duration_s)
                if not options:
                    reject("no_viable_onward_options")
                    return
                # Keep the preferred transit alternative and taxi for a concise UI.
                transits = [o for o in options if o.mode == "transit"]
                options = [o for o in options if o.mode == "taxi"]
                if transits:
                    options.append(preferred_option(transits, request.preferences.priority))
                road_name = dropoff_road_name(driver, place.point)
                if candidate.source == "roadside":
                    place = place.model_copy(update={"name": f"{road_name or '未命名道路'} · 下车候选"[:120]})
                identity = sha256(repr(candidate_key(candidate)).encode()).hexdigest()[:16]
                results.append(Recommendation(id=f"p-{identity}", point=place,
                    dropoff_road=road_name,
                    driver_detour_s=extra, driving_extra_s=driving_extra, driver_route=driver,
                    highway_exit=used_exit_place, original_exit=used_original_exit,
                    shared_ride_s=shared, stop_duration_s=stop_s, candidate_source=candidate.source,
                    options=options, suggested_mode=options[0].mode, score=0,
                    reason="连续途经路线估算；到P时间按途经路段内进度估算，停靠条件待核实"))
                event("candidate_accepted", recommendation=recommendation_summary(results[-1]), **candidate_trace(candidate))
            except asyncio.CancelledError:
                reject("evaluation_cancelled_or_budget_exhausted")
                raise
            except MapServiceError as exc:
                reject("map_service_error", message=str(exc))
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
                if (hub_first and offset + 8 >= 24 and len(results) >= 6
                        and set(request.preferences.modes) <= covered_modes
                        and required_regions <= evaluated_regions):
                    # A bounded shortlist is not an optimality claim; report remaining work below.
                    break
                # With taxi enabled, continue across the bounded candidate pool:
                # six feasible points do not establish the detour/fare frontier.
                if ("taxi" not in pools and len(results) >= 6
                        and set(request.preferences.modes) <= covered_modes
                        and required_regions <= evaluated_regions):
                    break
            # Refine around the station actually used by successful itineraries.
            # This is separate from arbitrary nearby station POIs discovered earlier.
            if "transit" in pools and not focused_exits and not hub_first:
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
        planning_budget_s = min(60, max(1, 105 - (perf_counter() - planning_started)))
        try:
            await asyncio.wait_for(plan(), timeout=planning_budget_s)
        except asyncio.TimeoutError:
            complete = False
            event("planning_budget_reached", budget_s=round(planning_budget_s, 1), evaluated_candidates=evaluated_count,
                  completed_candidates=len(results))
            warnings.append(f"计算达到本阶段{planning_budget_s:.0f}秒预算，仅返回已完整验证的部分方案；对照方案可能未完成。")
        if errors:
            complete = False
            if not results and not comparisons:
                raise errors[0]
            warnings.append("部分交通方案查询失败，本次推荐不完整，请稍后重试。")
        ranked = rank_recommendations(results, request.preferences)
        for rec in ranked:
            chosen = preferred_option([o for o in rec.options if o.mode == rec.suggested_mode], request.preferences.priority)
            alternatives = [(comparison.label, o) for comparison in comparisons for o in comparison.options]
            if rec.suggested_mode == "taxi":
                alternatives = [(label, o) for label, o in alternatives
                                if o.mode == "taxi" and o.fare_yuan is not None and o.fare_yuan >= 0]
                if alternatives and chosen.fare_yuan is not None:
                    label, alternative = min(alternatives, key=lambda pair: (
                        taxi_score_seconds(0, pair[1]), pair[1].total_duration_s, pair[1].fare_yuan))
                    delta = chosen.fare_yuan - alternative.fare_yuan
                    time_delta = chosen.total_duration_s - alternative.total_duration_s
                    rec.comparison = (f"相较于{label}，乘客预计{'多花' if delta > 0 else '少花'}"
                                      f"{abs(delta):.1f}元，全程{'多用' if time_delta > 0 else '少用'}"
                                      f"{abs(time_delta) / 60:.1f}分钟；本方案司机增加约"
                                      f"{rec.driver_detour_s / 60:.1f}分钟（含停靠）。")
                continue
            if alternatives:
                label, alternative = min(alternatives, key=lambda pair: option_score(pair[1], request.preferences.priority))
                delta = chosen.total_duration_s - alternative.total_duration_s
                rec.comparison = f"相较于{label}，预计{'多用' if delta > 0 else '节省'}{abs(delta) / 60:.1f}分钟；费用及步行请结合对照比较。"
                if option_score(alternative, request.preferences.priority) <= option_score(chosen, request.preferences.priority):
                    rec.comparison += " 按当前偏好，对照方案更优或相当，建议优先考虑。"
        if "taxi" in request.preferences.modes:
            warnings.append(f"乘客打车费用上限为{request.preferences.max_taxi_fare_yuan}元；超过上限或费用未知的打车选项不参与推荐。")
            warnings.append("打车综合分＝司机增加分钟＋乘客全程分钟＋预估车费×3，越低越优；多花1元可减少双方合计至少3分钟时优先省时。双方时间均含相应停靠，乘客时间含共同乘车、接车缓冲及后续驾车。")
            warnings.append("打车已取消原先多绕1分钟省3元的独立淘汰规则，统一执行时间与费用综合比较；推荐偏好仅影响公交方案。")
        if request.preferences.priority == "cost" and "transit" in request.preferences.modes:
            warnings.append("公共交通省费用仅比较不比当前最快公交候选多耗时15分钟以上的方案；未知费用不视为低价。")
        if not comparisons:
            warnings.append("本次未取得符合接驳约束的对照方案，无法判断中途下车是否优于直接出行。")
        if evaluated_count < len(candidates):
            complete = False
            for candidate in candidates[evaluated_count:]:
                event("candidate_not_evaluated", reason="early_stop_or_planning_budget", **candidate_trace(candidate))
            warnings.append("已保留足够可用候选，剩余候选尚未精算，推荐不代表全局最优。")
        if not ranked:
            warnings.append("已搜索范围内暂无符合限制的候选，不代表所有路线均无方案；公交接驳不会自动放宽300米限制。" if "transit" in request.preferences.modes else "已搜索范围内暂无同时满足司机增加时间和乘客打车费用上限的候选；打车不要求靠近公交或地铁。")
        generated = datetime.now(timezone.utc)
        return RecommendationResponse(mode="live", baseline_route=baseline, recommendations=ranked,
            comparisons=comparisons, warnings=warnings, generated_at=generated, departure_at=departure,
            expires_at=generated + timedelta(minutes=5), examined_candidates=evaluated_count, search_complete=complete and evaluated_count == len(candidates))

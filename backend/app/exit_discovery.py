"""Search driver and passenger corridors, then verify every complete driver trip."""
import asyncio
from bisect import bisect_right
from dataclasses import dataclass, field

from .candidates import (Candidate, Station, build_candidates, controlled_segment,
                         interleave_groups, restricted_segment, terminal_is_usable)
from .geometry import haversine_m, point_along_path, project_to_path
from .highway import ExitAnchor, discovered_exit, exit_was_used, highway_centers, original_exits
from .models import Place
from .search_log import event


def closest_location(route, destination):
    point, progress, gap = project_to_path(destination, route.polyline)
    # Use segment progress rather than a nearby parallel surface road's distance.
    walked = 0.0
    controlled = False
    for segment in route.segments:
        length = sum(haversine_m(a, b) for a, b in zip(segment.polyline, segment.polyline[1:]))
        if segment.polyline and walked - 1 <= progress <= walked + length + 1:
            controlled |= controlled_segment(segment)
        walked += length
    return point, progress, gap, controlled


def exits_around_q(anchors, progress):
    before = sorted((a for a in anchors if a.progress_m <= progress),
                    key=lambda a: -a.progress_m)[:3]
    after = sorted((a for a in anchors if a.progress_m > progress),
                   key=lambda a: a.progress_m)[:3]
    return interleave_groups([before, after])


class RouteIndex:
    """Index distances once; compare a prefix without searching later crossings."""
    def __init__(self, route):
        self.route = route
        self.points = []
        self.distances = []
        self.roads = []
        self.road_ends = []
        total = 0.0
        for segment in route.segments:
            if not segment.polyline:
                continue
            for point in segment.polyline:
                if self.points:
                    total += haversine_m(self.points[-1], point)
                self.points.append(point)
                self.distances.append(total)
            self.roads.append(segment)
            self.road_ends.append(total)
        self.length = total

    def point(self, distance):
        index = max(0, min(len(self.points) - 2, bisect_right(self.distances, distance) - 1))
        return point_along_path(self.points[index:index + 2], distance - self.distances[index])

    def road(self, distance):
        index = min(len(self.roads) - 1, bisect_right(self.road_ends, distance))
        return self.roads[index]


def common_prefix_end(driver, passenger):
    """Require continuous matching geometry, road names, and road level from E."""
    left, right = RouteIndex(driver), RouteIndex(passenger)
    if min(len(left.points), len(right.points)) < 2:
        return None
    maximum = min(left.length, right.length)

    def same(distance):
        a, b = left.road(distance), right.road(distance)
        return (bool(a.name) and a.name == b.name
                and controlled_segment(a) == controlled_segment(b)
                and haversine_m(left.point(distance), right.point(distance)) <= 8)

    if not same(0):
        # Some exits split immediately into different named surface roads.
        return 0.0 if haversine_m(left.point(0), right.point(0)) <= 5 else None
    previous = 0.0
    for distance in range(20, int(maximum) + 20, 20):
        distance = min(distance, maximum)
        if not same(distance):
            low, high = previous, distance
            while high - low > 1:
                middle = (low + high) / 2
                if same(middle):
                    low = middle
                else:
                    high = middle
            return low
        previous = distance
    return maximum


def ordinary_near_split(route, progress, before):
    """Nearest known ordinary point, up to 2 km before/after the split."""
    index = RouteIndex(route)
    if len(index.points) < 2:
        return None
    for offset in (15, 30, 60, 100, 200, 400, 800, 1200, 2000):
        distance = progress - offset if before else progress + offset
        if not 0 <= distance <= index.length:
            continue
        segment = index.road(distance)
        if not segment.name or restricted_segment(segment):
            continue
        point = index.point(distance)
        probe = route.model_copy(update={"polyline": [point]})
        if terminal_is_usable(probe, point, True):
            return point
    return None


@dataclass
class FocusedDiscovery:
    candidates: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    complete: bool = True
    failed: int = 0
    succeeded: int = 0

    async def run(self, api, request, routes):
        self.api, self.request, self.routes = api, request, routes
        self.groups = []
        self.destination_groups = []
        self.station_groups = []
        self.split_points = []
        self.stations = {}
        self.seen = {}
        try:
            await asyncio.wait_for(self.discover(), timeout=38)
        except asyncio.TimeoutError:
            self.complete = False
            self.warnings.append("出口及分流位置搜索达到38秒预算，保留已完成候选；部分出口或站点尚未核验。")
        # Give every exit and transport mode a turn before using the 48 slots.
        taxi = interleave_groups([interleave_groups(self.groups),
                                  interleave_groups(self.destination_groups)])
        transit = interleave_groups([
            interleave_groups([g for g in self.station_groups if g[0].discovery_group != "passenger_split"]),
            interleave_groups([g for g in self.station_groups if g[0].discovery_group == "passenger_split"])])
        for candidate in interleave_groups([taxi, transit]):
            key = (candidate.place.point.csv(), candidate.approach.csv() if candidate.approach else "",
                   candidate.exit_place.point.csv() if candidate.exit_place else "")
            if key in self.seen:
                continue
            self.seen[key] = candidate
            self.candidates.append(candidate)
            if len(self.candidates) >= 48:
                self.complete = False
                break
        if self.failed:
            self.complete = False
            self.warnings.append("部分出口或接驳查询失败，本次搜索覆盖不完整。")
        return self

    async def call(self, method, *args, **kwargs):
        # Imported lazily to avoid the adapter/discovery import cycle.
        from .amap import MapServiceError
        try:
            result = await method(*args, **kwargs)
            self.succeeded += 1
            return result
        except MapServiceError:
            self.failed += 1
            return None

    async def discover(self):
        destination_routes = []
        try:
            route = await asyncio.wait_for(self.call(self.api.driving, self.request.origin.point,
                                                     self.request.passenger_destination.point), timeout=8)
            if route is not None and route.polyline:
                # Capture choices before concurrent exit queries overwrite the adapter's list.
                choices = list(self.api.driving_choices) or [route]
                destination_routes = sorted((r for r in choices if r.polyline),
                                            key=lambda r: r.duration_s)[:2]
            else:
                self.complete = False
                self.warnings.append("未取得A→C探索路线，仍保留A→B沿途候选。")
        except asyncio.TimeoutError:
            self.complete = False
            self.warnings.append("A→C探索路线查询达到预算，仍保留A→B沿途候选。")
        event("destination_corridors_ready", strategy="exit_split", count=len(destination_routes),
              routes=[dict(duration_s=r.duration_s, distance_m=r.distance_m) for r in destination_routes])
        queries = []
        contexts = []
        for route in self.routes:
            q, progress, gap, controlled = closest_location(route, self.request.passenger_destination.point)
            anchors = original_exits(route, self.request.origin.citycode)
            contexts.append((route, q, progress, controlled, anchors))
            event("closest_route_point", point=q.model_dump(), progress_m=round(progress),
                  distance_to_destination_m=round(gap), controlled=controlled)
            if controlled:
                centers = sorted(highway_centers(route),
                                 key=lambda p: abs(project_to_path(p, route.polyline)[1] - progress))
                # Search both sides of Q in route order, not just around C.
                before = [p for p in centers if project_to_path(p, route.polyline)[1] <= progress]
                after = [p for p in centers if project_to_path(p, route.polyline)[1] > progress]
                queries.append([q, *interleave_groups([before, after])])
        destination_contexts = []
        destination_queries = []
        for route in destination_routes:
            anchors = original_exits(route, self.request.passenger_destination.citycode)
            for anchor in anchors:
                anchor.original = False
            destination_contexts.append((route, anchors))
            # A->C ends at C, usually on an ordinary road. Search its controlled
            # sections explicitly instead of requiring the closest point Q to be controlled.
            centers_near_c = sorted(highway_centers(route),
                                    key=lambda p: haversine_m(p, self.request.passenger_destination.point))
            destination_queries.append(centers_near_c)
        centers = []
        # Reserve discovery coverage for both corridors within the existing budget.
        for point in interleave_groups([interleave_groups(queries)[:16],
                                        interleave_groups(destination_queries)[:8]]):
            if all(haversine_m(point, old) >= 500 for old in centers):
                centers.append(point)
            if len(centers) == 24:
                break

        async def find_exits(point):
            places = await self.call(self.api.nearby, point, "出口", radius=1500)
            for place in places or []:
                for route, _, _, controlled, anchors in contexts:
                    if controlled:
                        anchor = discovered_exit(route, place)
                        if anchor and not any(haversine_m(anchor.place.point, a.place.point) < 80 for a in anchors):
                            anchors.append(anchor)
                for route, anchors in destination_contexts:
                    anchor = discovered_exit(route, place)
                    if anchor and not any(haversine_m(anchor.place.point, a.place.point) < 80 for a in anchors):
                        anchor.original = False
                        anchors.append(anchor)
        try:
            await asyncio.wait_for(asyncio.gather(*(find_exits(p) for p in centers)), timeout=12)
        except asyncio.TimeoutError:
            self.complete = False
            self.warnings.append("出口发现达到预算，Q前后各三个出口可能未找齐。")
        jobs = []
        for route, q, progress, controlled, anchors in contexts:
            if controlled:
                selected = exits_around_q(anchors, progress)
                event("exits_around_q", q=q.model_dump(),
                      exits=[dict(point=a.place.point.model_dump(), name=a.place.name,
                                  side="before" if a.progress_m <= progress else "after") for a in selected])
                if sum(a.progress_m <= progress for a in selected) < 3 or sum(a.progress_m > progress for a in selected) < 3:
                    self.complete = False
                    self.warnings.append("部分路线在Q前后未各找到三个出口，按已发现且可核验的出口继续搜索。")
                jobs.extend(self.explore_exit(route, anchor) for anchor in selected)
            else:
                jobs.append(self.explore_surface(route, q, progress))
        for route, anchors in destination_contexts:
            # One landing may have several POI labels. Keep three distinct exit areas.
            selected = []
            for anchor in sorted(anchors, key=lambda a: haversine_m(
                    a.place.point, self.request.passenger_destination.point)):
                if any(haversine_m(anchor.place.point, old.place.point) < 300 for old in selected):
                    continue
                selected.append(anchor)
                if len(selected) == 3:
                    break
            event("destination_exits_selected", exits=[a.place.model_dump() for a in selected])
            if not selected and any(controlled_segment(s) for s in route.segments):
                self.complete = False
                self.warnings.append("部分A→C探索路线未找到可核验出口，乘客方向覆盖不完整。")
            jobs.extend(self.explore_exit(route, a, "passenger_split") for a in selected)
        await asyncio.gather(*jobs)
        if "transit" in self.request.preferences.modes:
            await self.discover_stations()

    def candidate(self, route, point, anchor=None, source="roadside", modes=None, citycode=None):
        _, progress, _ = project_to_path(point, route.polyline)
        estimate = route.duration_s * progress / max(route.distance_m, 1)
        return Candidate(Place(name="分流附近下车候选", point=point,
                               citycode=citycode or (anchor.place.citycode if anchor else None)
                               or self.request.origin.citycode),
                         source, progress, None, estimate,
                         exit_place=anchor.place if anchor else None,
                         approach=anchor.approach if anchor else None,
                         original_exit=anchor.original if anchor else False,
                         modes=tuple(modes or self.request.preferences.modes), discovery_group="split")

    async def explore_exit(self, baseline, anchor, discovery_group="split"):
        # Plan from A directly. Q is never a mandatory waypoint, including earlier exits.
        via = [anchor.approach, anchor.place.point]
        driven = await self.call(self.api.driving, self.request.origin.point,
                                 self.request.driver_destination.point, via=via)
        if driven is None or not driven.polyline:
            return
        _, approach_progress, approach_gap = project_to_path(anchor.approach, driven.polyline)
        _, exit_progress, exit_gap = project_to_path(anchor.place.point, driven.polyline)
        if approach_gap > 30 or exit_gap > 50 or approach_progress >= exit_progress:
            return
        # Find the actual surface-road landing used by the provider, not a ramp POI.
        landings = [a for a in original_exits(driven, anchor.place.citycode)
                    if a.progress_m > approach_progress
                    and haversine_m(a.place.point, anchor.place.point) <= 2500]
        landing = min(landings, key=lambda a: a.progress_m, default=None)
        if landing is None:
            event("exit_discovery_rejected", point=anchor.place.point.model_dump(), reason="no_surface_landing")
            return
        # The original approach and the verified landing constrain final A->P->B too.
        place = landing.place.model_copy(update={"name": landing.place.name.replace(
            "原路线驶出后：", "乘客方向驶出后：" if discovery_group == "passenger_split" else "实际驶出后：")})
        actual = ExitAnchor(place, anchor.approach, anchor.progress_m, anchor.original)
        to_b, to_c = await asyncio.gather(
            self.call(self.api.driving, landing.place.point, self.request.driver_destination.point),
            self.call(self.api.driving, landing.place.point, self.request.passenger_destination.point))
        if to_b is None or to_c is None:
            return
        split = common_prefix_end(to_b, to_c)
        if split is None:
            self.complete = False
            return
        group = []
        for branch, before in ((to_b, True), (to_b, False), (to_c, False)):
            point = ordinary_near_split(branch, split, before)
            if point is None or any(haversine_m(point, c.place.point) < 5 for c in group):
                continue
            if not exit_was_used(driven, actual, point):
                # C-branch points need not occur on the E->B route; final verification
                # checks the complete constrained route for every branch individually.
                _, landing_progress, gap = project_to_path(landing.place.point, branch.polyline)
                _, point_progress, _ = project_to_path(point, branch.polyline)
                if gap > 30 or point_progress <= landing_progress:
                    continue
            candidate = self.candidate(baseline, point, actual)
            candidate.discovery_group = discovery_group
            group.append(candidate)
            self.split_points.append((baseline, candidate))
        if group:
            (self.destination_groups if discovery_group == "passenger_split" else self.groups).append(group)
        event("exit_split_discovered", exit=actual.place.model_dump(),
              discovery_group=discovery_group,
              split_point=RouteIndex(to_b).point(split).model_dump(),
              candidates=[c.place.point.model_dump() for c in group])

    async def explore_surface(self, route, q, progress):
        # Ordinary roads keep along-route discovery; include Q explicitly.
        parking = await self.call(self.api.nearby, q, "停车场") or []
        candidates = build_candidates(route, parking, [], self.request, limit=12)
        if terminal_is_usable(route.model_copy(update={"polyline": [q]}), q, True):
            candidates.insert(0, self.candidate(route, q))
        self.groups.append(candidates)
        self.split_points.extend((route, c) for c in candidates[:3])

    async def discover_stations(self):
        # Each split-area candidate gets a 5-km station search; access is by car,
        # and the existing walking API still enforces the actual 300-m entrance cap.
        async def find(route, candidate, kind):
            places = await self.call(self.api.nearby, candidate.place.point, kind, radius=5000)
            selected = []
            for place in places or []:
                if haversine_m(place.point, candidate.place.point) > 5000:
                    continue
                station = Station(place, "bus" if kind == "公交站" else "subway")
                key = (place.point.csv(), candidate.exit_place.point.csv() if candidate.exit_place else "")
                if key not in self.stations:
                    self.stations[key] = (route, candidate, station)
                    selected.append(key)
            return selected
        areas = interleave_groups([
            [item for item in self.split_points if item[1].discovery_group != "passenger_split"],
            [item for item in self.split_points if item[1].discovery_group == "passenger_split"]])
        jobs = [find(route, candidate, kind) for route, candidate in areas
                for kind in ("地铁站", "公交站")]
        groups = await asyncio.gather(*jobs)
        # Round-robin by area and station kind so one dense bus area cannot take all slots.
        keys = list(dict.fromkeys(interleave_groups(groups)))
        if len(keys) > 12:
            self.complete = False
        await asyncio.gather(*(self.station_approach(*self.stations[key]) for key in keys[:12]))

    async def station_approach(self, baseline, candidate, station):
        via = [candidate.approach, candidate.exit_place.point] if candidate.exit_place else None
        route = await self.call(self.api.driving, self.request.origin.point, station.place.point, via=via)
        if route is None or not route.polyline:
            return
        length = RouteIndex(route).length
        group = []
        for offset in (15, 40, 80, 150, 250):
            point = ordinary_near_split(route, max(0, length - offset + 15), True)
            if point is None or haversine_m(point, station.place.point) > 300:
                continue
            anchor = (ExitAnchor(candidate.exit_place, candidate.approach, candidate.progress_m,
                                 candidate.original_exit) if candidate.exit_place else None)
            if anchor and not exit_was_used(route, anchor, point):
                continue
            found = self.candidate(baseline, point, anchor, "station_area", ("transit",), station.place.citycode)
            found.discovery_group = candidate.discovery_group
            found.place.name = f"{station.place.name}附近道路"[:120]
            group.append(found)
            if len(group) == 2:
                break
        if group:
            self.station_groups.append(group)

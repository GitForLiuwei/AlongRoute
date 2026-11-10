"""Hub-first recall, cheap ranking, then real surface-road approaches.

Exit/split exploration is a bounded supplement, not the primary candidate source.
"""
import asyncio
from dataclasses import dataclass, field
from hashlib import sha256
from time import perf_counter

from .candidates import (Candidate, build_candidates, interleave_groups, restricted_road,
                         terminal_is_usable, controlled_segment)
from .coarse_routing import estimate_candidates, geometry_estimate, coverage_order
from .exit_discovery import FocusedDiscovery
from .geometry import haversine_m, point_along_path, project_to_path, sample_path
from .highway import original_exits
from .hub_store import hub_store
from .preselection import select_candidates
from .search_log import event


@dataclass
class HubDiscovery:
    candidates: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    complete: bool = False  # Bounded POI pages/corridors never establish exhaustive coverage.
    failed: int = 0
    succeeded: int = 0
    seeds: dict = field(default_factory=dict)
    approached: list = field(default_factory=list)
    budget_s: float = 38

    async def run(self, api, request, routes):
        from .amap import MapServiceError
        self.api, self.request = api, request
        deadline = perf_counter() + self.budget_s
        passenger_routes = []
        try:
            route = await asyncio.wait_for(api.driving(request.origin.point, request.passenger_destination.point),
                                           timeout=min(8, self.budget_s))
            if route and route.polyline:
                passenger_routes = sorted((r for r in (list(api.driving_choices) or [route]) if r.polyline),
                                          key=lambda r: r.duration_s)[:2]
        except (MapServiceError, asyncio.TimeoutError):
            self.warnings.append('乘客方向探索未完成，仍保留司机路线和目的地周边枢纽。')
        exploration = routes + passenger_routes
        self.routes = routes
        surface = []
        for index, route in enumerate(exploration):
            group = build_candidates(route, [], [], request, limit=12)
            for candidate in group:
                candidate.route_index = index
                candidate.discovery_group = 'hub_surface'
                candidate.region = f'surface:{index}:{int(candidate.progress_m // 5000)}'
                candidate.origins = ['baseline_surface' if index < len(routes) else 'passenger_surface']
                candidate.modes = tuple(request.preferences.modes)
            surface.extend(group)
        max_corridor_m = max(8000.0, request.preferences.max_detour_minutes * 800.0)
        centers = recall_centers(exploration, request.passenger_destination.point,
                                 driver_routes=routes, max_corridor_m=max_corridor_m)
        event('discovery_map', strategy='hub_first', driver_routes=[overview(r) for r in routes],
              passenger_routes=[overview(r) for r in passenger_routes],
              search_centers=[p.model_dump() for p in centers], search_radius_m=5000)
        event('destination_corridors_ready', strategy='hub_first', count=len(passenger_routes))
        kinds = [('停车场', 'parking'), ('地铁站', 'subway')]
        if 'transit' in request.preferences.modes:
            kinds.append(('公交站', 'bus'))
        try:
            await asyncio.wait_for(asyncio.gather(*(self.fetch(center, label, kind, index)
                for index, center in enumerate(centers) for label, kind in kinds)),
                timeout=max(.01, min(13, deadline - perf_counter())))
        except asyncio.TimeoutError:
            self.warnings.append('枢纽搜索达到时间预算，保留已取得的站点和普通道路。')
        seeds = sorted(self.seeds.values(), key=lambda c: (c.region, c.hub_kind, c.place.point.csv()))
        for seed in seeds:
            seed.coarse = geometry_estimate(seed, routes, request.passenger_destination.point)
        max_detour_s = request.preferences.max_detour_minutes * 60
        seeds = coverage_order(seeds, request.passenger_destination.point,
                               request.preferences.modes, max_detour_s=max_detour_s)[:96]
        ordered = await estimate_candidates(api, request, routes, seeds)
        event('hub_shortlist', generated=len(self.seeds), selected=min(12, len(ordered)))
        try:
            await asyncio.wait_for(asyncio.gather(*(self.approach(seed) for seed in ordered[:12])),
                timeout=max(.01, min(12, deadline - perf_counter())))
        except asyncio.TimeoutError:
            self.warnings.append('部分枢纽的下客道路尚未完成核验，保留已生成候选。')
        primary = sorted(self.approached, key=lambda c: (c.region, c.hub_kind, c.place.point.csv()))
        # Count useful hub coverage before deciding whether exit exploration is needed.
        for c in primary:
            c.coarse = geometry_estimate(c, routes, request.passenger_destination.point)
        soft_detour_bound_s = max(1200.0, max_detour_s * 3.0)
        feasible_primary = [c for c in primary if (c.coarse.get('detour_s') or 0) <= soft_detour_bound_s]
        needs_supplement = len(feasible_primary) < 8 or (
            'transit' in request.preferences.modes and not any(c.source == 'station_area' for c in primary)
        )
        if needs_supplement and any(controlled_segment(s) for r in routes for s in r.segments) and deadline - perf_counter() > 2:
            supplement = await FocusedDiscovery(candidate_limit=8, surface_reserve=0,
                discovery_budget_s=min(8, deadline - perf_counter()), include_stations=False,
                passenger_routes=passenger_routes, exit_limit=2, query_limit=6).run(api, request, routes)
            primary.extend(supplement.candidates)
            self.failed += supplement.failed
            self.succeeded += supplement.succeeded
            event('hub_exit_supplement', count=len(supplement.candidates))
        if self.failed and not self.succeeded and not primary and not surface:
            raise MapServiceError('枢纽及接驳地点查询失败，请稍后重试')
        # Only a bounded matrix stage remains; completed approaches retain useful geometry on timeout.
        for c in [*primary, *surface]:
            c.coarse = geometry_estimate(c, routes, request.passenger_destination.point)
        try:
            ordered = await asyncio.wait_for(estimate_candidates(api, request, routes, [*primary, *surface]),
                                            timeout=max(.01, min(6.0, deadline - perf_counter())))
        except asyncio.TimeoutError:
            ordered = coverage_order([*primary, *surface], request.passenger_destination.point,
                                     request.preferences.modes, max_detour_s=max_detour_s)
        primary_ids = {id(c) for c in primary}
        self.candidates, _ = select_candidates([c for c in ordered if id(c) in primary_ids],
            [c for c in ordered if id(c) not in primary_ids], limit=48, surface_reserve=12)
        self.warnings.append('优先搜索地铁、公交枢纽及停车场，补充沿途普通道路；枢纽只是搜索锚点，最终下客位置仍核验完整路线。')
        if self.failed:
            self.warnings.append('部分枢纽或接近路线查询失败，搜索覆盖不完整。')
        engines = {c.coarse.get('engine') for c in self.candidates}
        if 'geometry' in engines:
            self.warnings.append('部分或全部候选采用几何粗排；粗估只决定核验顺序，不作为可达性、费用或绕行达标结论。')
        return self

    async def fetch(self, center, label, kind, region):
        from .amap import MapServiceError
        namespace = sha256(self.api.key.encode()).hexdigest()
        cached, covered = hub_store.lookup(namespace, center, kind, 5000)
        places = list(cached)
        try:
            if not covered:
                fresh = await self.api.nearby(center, label, radius=5000)
                hub_store.put(namespace, center, kind, 5000, fresh)
                places.extend(fresh)
            self.succeeded += 1
        except MapServiceError:
            self.failed += 1
        finally:
            # Harvest cached seeds even if provider work is cancelled.
            unique = {p.point.csv(): p for p in places
                      if haversine_m(center, p.point) <= 5000 and not restricted_road(p.name)}
            driver_routes = getattr(self, 'routes', None) or []

            def seed_priority(p):
                entrance_rank = 0 if (kind == 'subway' and '口' in p.name) else 1
                if driver_routes:
                    corridor_gap = min((project_to_path(p.point, r.polyline)[2] for r in driver_routes if r.polyline),
                                       default=haversine_m(center, p.point))
                    return (entrance_rank, round(corridor_gap, -2), haversine_m(center, p.point), p.point.csv())
                return (haversine_m(center, p.point), p.point.csv())

            places = sorted(unique.values(), key=seed_priority)[:25]
            for place in places:
                key = (kind, place.point.csv())
                candidate = Candidate(place, 'parking' if kind == 'parking' else 'station_area', 0, None, 0,
                    modes=tuple(self.request.preferences.modes), discovery_group='hub', hub_kind=kind,
                    region=f'hub:{region}', origins=[f'hub_{kind}'])
                old = self.seeds.get(key)
                if old is None or candidate.region < old.region:
                    self.seeds[key] = candidate
            event('hub_seeds', hub_kind=kind, center=center.model_dump(), cache=covered,
                  places=[p.model_dump(mode='json') for p in places[:25]])

    async def approach(self, seed):
        from .amap import MapServiceError
        try:
            route = await self.api.driving(self.request.origin.point, seed.place.point)
            self.succeeded += 1
        except MapServiceError:
            self.failed += 1
            return
        if not route or not route.polyline:
            return
        length = sum(haversine_m(a, b) for a, b in zip(route.polyline, route.polyline[1:]))
        points = ([seed.place.point] if seed.hub_kind == 'parking' else []) + [
            point_along_path(route.polyline, max(0, length-offset)) for offset in (15, 60, 150, 250)]
        found = []
        for point in points:
            if haversine_m(point, seed.place.point) > 300 or not terminal_is_usable(
                    route.model_copy(update={'polyline': [point]}), point, True):
                continue
            if any(haversine_m(point, c.place.point) < 5 for c in found):
                continue
            exact_parking = seed.hub_kind == 'parking' and point == seed.place.point
            place = seed.place.model_copy(update={'point': point, 'poi_id': seed.place.poi_id if exact_parking else None,
                'name': seed.place.name if exact_parking else f'{seed.place.name}附近道路'[:120]})
            candidate = Candidate(place, 'parking' if exact_parking else ('station_area' if seed.hub_kind != 'parking' else 'roadside'),
                0, haversine_m(point, seed.place.point), route.duration_s,
                modes=tuple(self.request.preferences.modes), discovery_group='hub', hub_kind=seed.hub_kind,
                region=seed.region, origins=seed.origins[:])
            found.append(candidate)
            if len(found) == 2:
                break
        self.approached.extend(found)


def recall_centers(routes, destination, limit=6, driver_routes=None, max_corridor_m=None):
    driver_list = driver_routes or []

    def within_corridor(pt):
        if not driver_list or max_corridor_m is None:
            return True
        return min((project_to_path(pt, r.polyline)[2] for r in driver_list if r.polyline), default=0.0) <= max_corridor_m

    dest_near = within_corridor(destination)
    groups = [[destination]] if dest_near else []
    for route in routes:
        q, _, _ = project_to_path(destination, route.polyline)
        exits = [a.place.point for a in original_exits(route)]
        samples = sample_path(route.polyline, 8)
        ordered_pts = [q, *sorted(exits + samples, key=lambda p: haversine_m(p, destination))]
        filtered_pts = [p for p in ordered_pts if within_corridor(p)] or [q]
        groups.append(filtered_pts)
    if not dest_near:
        groups.append([destination])
    centers = []
    for point in interleave_groups(groups):
        if all(haversine_m(point, old) >= 2000 for old in centers):
            centers.append(point)
        if len(centers) >= limit:
            break
    return centers


def overview(route):
    points = route.polyline[::max(1, (len(route.polyline)+198)//199)]
    if points and points[-1] != route.polyline[-1]:
        points.append(route.polyline[-1])
    return [p.model_dump() for p in points]

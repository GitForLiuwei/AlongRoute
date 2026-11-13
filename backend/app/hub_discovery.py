"""Hub-first recall, cheap ranking, then real surface-road approaches.

Exit/split exploration is a bounded supplement, not the primary candidate source.
"""
import asyncio
from dataclasses import dataclass, field
from hashlib import sha256
from time import perf_counter

from .candidates import (Candidate, build_candidates, interleave_groups, restricted_road,
                        restricted_segment, terminal_is_usable, controlled_segment)
from .coarse_routing import estimate_candidates, geometry_estimate, coverage_order
from .exit_discovery import FocusedDiscovery
from .geometry import haversine_m, point_along_path, project_to_path, sample_path
from .highway import original_exits
from .hub_store import extract_transit_line_tokens, hub_store
from .models import Place
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
        length_cache = {}
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
        max_corridor_m = min(14000.0, max(8000.0, request.preferences.max_detour_minutes * 700.0))
        centers = recall_centers(exploration, request.passenger_destination.point, limit=6,
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
                                   timeout=max(.01, min(14, deadline - perf_counter())))
        except asyncio.TimeoutError:
            self.warnings.append('枢纽搜索达到时间预算，保留已取得的站点和普通道路。')
        seeds = sorted(self.seeds.values(), key=lambda c: (c.region, c.hub_kind, c.place.point.csv()))
        for seed in seeds:
            seed.coarse = geometry_estimate(seed, routes, request.passenger_destination.point,
                                            length_cache=length_cache)
        max_detour_s = request.preferences.max_detour_minutes * 60
        priority = getattr(request.preferences, 'priority', 'balanced') or 'balanced'
        seeds = coverage_order(seeds, request.passenger_destination.point,
                               request.preferences.modes, max_detour_s=max_detour_s, priority=priority)[:96]
        ordered = await estimate_candidates(api, request, routes, seeds)
        approach_limit = 10
        event('hub_shortlist', generated=len(self.seeds), selected=min(approach_limit, len(ordered)))
        try:
            await asyncio.wait_for(asyncio.gather(*(self.approach(seed) for seed in ordered[:approach_limit])),
                                   timeout=max(.01, min(14, deadline - perf_counter())))
        except asyncio.TimeoutError:
            self.warnings.append('部分枢纽的下客道路尚未完成核验，保留已生成候选。')
        primary = sorted(self.approached, key=lambda c: (c.region, c.hub_kind, c.place.point.csv()))
        for c in primary:
            c.coarse = geometry_estimate(c, routes, request.passenger_destination.point,
                                         length_cache=length_cache)
        soft_detour_bound_s = max(1200.0, min(2400.0, max_detour_s * 2.0))
        feasible_primary = [c for c in primary if (c.coarse.get('detour_s') or 0) <= soft_detour_bound_s]
        has_bridge_primary = any(
            2800 <= min((project_to_path(c.place.point, r.polyline)[2] for r in routes if r.polyline), default=0.0) <= 9500
            and haversine_m(c.place.point, request.passenger_destination.point) >= 4000
            for c in primary
        )
        needs_supplement = (
            len(feasible_primary) < 4
            or ('transit' in request.preferences.modes and not any(c.source == 'station_area' for c in primary))
        )
        if needs_supplement and any(controlled_segment(s) for r in routes for s in r.segments) and deadline - perf_counter() > 2:
            supplement = await FocusedDiscovery(candidate_limit=6, surface_reserve=0,
                                                 discovery_budget_s=min(8, deadline - perf_counter()), include_stations=False,
                                                 passenger_routes=passenger_routes, exit_limit=2, query_limit=6).run(api, request, routes)
            primary.extend(supplement.candidates)
            self.failed += supplement.failed
            self.succeeded += supplement.succeeded
            event('hub_exit_supplement', count=len(supplement.candidates))
        if self.failed and not self.succeeded and not primary and not surface:
            raise MapServiceError('枢纽及接驳地点查询失败，请稍后重试')
        for c in [*primary, *surface]:
            c.coarse = geometry_estimate(c, routes, request.passenger_destination.point,
                                         length_cache=length_cache)
        try:
            ordered = await asyncio.wait_for(estimate_candidates(api, request, routes, [*primary, *surface]),
                                             timeout=max(.01, min(6.0, deadline - perf_counter())))
        except asyncio.TimeoutError:
            ordered = coverage_order([*primary, *surface], request.passenger_destination.point,
                                     request.preferences.modes, max_detour_s=max_detour_s, priority=priority)
        primary_ids = {id(c) for c in primary}
        self.candidates, _ = select_candidates([c for c in ordered if id(c) in primary_ids],
                                                [c for c in ordered if id(c) not in primary_ids], limit=24, surface_reserve=6)
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
            dest_pt = self.request.passenger_destination.point
            ac_dist = haversine_m(self.request.origin.point, dest_pt)
            driver_routes = getattr(self, 'routes', None) or []
            dest_corridor_gap = min(
                (project_to_path(dest_pt, r.polyline)[2] for r in driver_routes if r.polyline),
                default=0.0,
            )
            is_far_dest_center = haversine_m(center, dest_pt) <= 1200 and ac_dist > 5000 and dest_corridor_gap > 3500
            unique = {p.point.csv(): p for p in places
                      if haversine_m(center, p.point) <= 5000 and not restricted_road(p.name)
                      and not (is_far_dest_center and haversine_m(p.point, dest_pt) < 900)}

            # Reverse Transit Isochrone Lookup: find subway lines serving passenger destination C in <1ms via R-Tree
            _dest_lines: set[str] = set()
            if kind == 'subway':
                try:
                    _dest_hubs, _ = hub_store.lookup('amap_v5', dest_pt, 'subway', 3500)
                    for _dh in _dest_hubs:
                        _dest_lines.update(extract_transit_line_tokens(_dh.name))
                except Exception:
                    pass

            def seed_priority(p):
                dist_center = haversine_m(center, p.point)
                dist_c = haversine_m(p.point, dest_pt)
                if is_far_dest_center:
                    core_tier = 0 if 1500 <= dist_c <= 4200 else 1
                else:
                    core_tier = 0 if dist_center <= 3000 else 1
                entrance_rank = 0 if (kind == 'subway' and '口' in p.name) else 1
                direct_line_rank = 0 if (_dest_lines and (extract_transit_line_tokens(p.name) & _dest_lines)) else 1
                return (core_tier, direct_line_rank, entrance_rank, round(dist_center, -2), p.point.csv())

            places = sorted(unique.values(), key=seed_priority)[:25]
            for place in places:
                key = (kind, place.point.csv())
                dist_center = haversine_m(center, place.point)
                dist_c = haversine_m(place.point, dest_pt)
                in_core = (1500 <= dist_c <= 4200) if is_far_dest_center else (dist_center <= 3000)
                if not in_core:
                    sub_region = f'hub:{region}:outer'
                elif kind == 'subway' and not is_far_dest_center:
                    side = 'w' if place.point.lon <= center.lon else 'e'
                    sub_region = f'hub:{region}:{side}'
                else:
                    sub_region = f'hub:{region}'
                if kind == 'bus' and 'transit' in self.request.preferences.modes and dist_c > 800:
                    seed_modes = ('transit',)
                else:
                    seed_modes = tuple(self.request.preferences.modes)
                candidate = Candidate(place, 'parking' if kind == 'parking' else 'station_area', 0, None, 0,
                                      modes=seed_modes, discovery_group='hub', hub_kind=kind,
                                      region=sub_region, origins=[f'hub_{kind}'])
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

        # U-turn avoidance: if one of the final 4 segments contains a U-turn ("掉头"),
        # extract the point on the forward carriageway immediately before the U-turn!
        pre_uturn_points = []
        segs = list(route.segments or [])
        for idx in range(max(0, len(segs) - 4), len(segs)):
            seg = segs[idx]
            action = ' '.join((seg.instruction, seg.end_action, seg.assistant_action))
            if any(word in action for word in ("掉头", "调头")) and idx > 0:
                prev_seg = segs[idx - 1]
                if prev_seg.polyline and prev_seg.name and not restricted_segment(prev_seg):
                    pre_pt = prev_seg.polyline[-1]
                    if haversine_m(pre_pt, seed.place.point) <= 320:
                        pre_uturn_points.append(pre_pt)

        points = (
            pre_uturn_points
            + ([seed.place.point] if seed.hub_kind == 'parking' else [])
            + [point_along_path(route.polyline, max(0, length - offset)) for offset in (15, 60, 150, 250, 350)]
        )
        for seg in reversed(segs):
            if seg.polyline and seg.name and not restricted_segment(seg):
                points.extend([seg.polyline[-1], seg.polyline[len(seg.polyline) // 2]])
                break
        found = []
        for point in points:
            max_radius = 300 if len(found) > 0 else 450
            if haversine_m(point, seed.place.point) > max_radius or not terminal_is_usable(
                    route.model_copy(update={'polyline': [point]}), point, True):
                continue
            if any(haversine_m(point, c.place.point) < 5 for c in found):
                continue
            exact_parking = seed.hub_kind == 'parking' and point == seed.place.point
            place = seed.place.model_copy(update={'point': point, 'poi_id': seed.place.poi_id if exact_parking else None,
                                                  'name': seed.place.name if exact_parking else f'{seed.place.name}附近道路'[:120]})
            candidate = Candidate(place, 'parking' if exact_parking else ('station_area' if seed.hub_kind != 'parking' else 'roadside'),
                                  0, haversine_m(point, seed.place.point), route.duration_s,
                                  modes=seed.modes, discovery_group='hub', hub_kind=seed.hub_kind,
                                  region=seed.region, origins=seed.origins[:])
            candidate.approach_duration_s = route.duration_s
            found.append(candidate)
            if len(found) == 2:
                break

        # Capture immediate post-highway-exit surface road point (e.g. right outside Qinghu Toll Station)
        if 'taxi' in self.request.preferences.modes and route.segments:
            last_ctrl_name = None
            walked_s = 0.0
            for seg in route.segments:
                seg_dur = getattr(seg, 'duration_s', 0) or 0
                if not seg.polyline:
                    walked_s += seg_dur
                    continue
                if controlled_segment(seg) or any(w in (seg.name + ' ' + seg.instruction) for w in ('收费站', '高速', '立交')):
                    if seg.name:
                        last_ctrl_name = seg.name
                    walked_s += seg_dur
                elif last_ctrl_name is not None and seg.name and not restricted_segment(seg):
                    seg_len = sum(haversine_m(a, b) for a, b in zip(seg.polyline, seg.polyline[1:]))
                    exit_pt = point_along_path(seg.polyline, min(60.0, seg_len * 0.5)) if seg_len > 10 else seg.polyline[0]
                    if (haversine_m(exit_pt, seed.place.point) > 450
                            and haversine_m(exit_pt, self.request.passenger_destination.point) >= 1500
                            and terminal_is_usable(route.model_copy(update={'polyline': [exit_pt]}), exit_pt, True)
                            and not any(haversine_m(exit_pt, c.place.point) < 100 for c in self.approached + found)):
                        base_reg = ':'.join(seed.region.split(':')[:2])
                        exit_place = Place(
                            name=f'{last_ctrl_name}出口 · {seg.name}附近'[:120],
                            point=exit_pt,
                            citycode=seed.place.citycode or self.request.origin.citycode,
                        )
                        exit_cand = Candidate(
                            exit_place, 'roadside', 0, None, max(60.0, walked_s + 15.0),
                            modes=tuple(self.request.preferences.modes),
                            discovery_group='hub', hub_kind='parking',
                            region=base_reg, origins=['highway_exit_approach'],
                        )
                        exit_cand.approach_duration_s = max(60.0, walked_s + 15.0)
                        found.append(exit_cand)
                    break
                else:
                    walked_s += seg_dur
        self.approached.extend(found)


def _highway_gate_points(route) -> list:
    """Extract actual highway interchanges, toll stations, and exit transitions from route segments."""
    gates = [a.place.point for a in original_exits(route)]
    prev_ctrl_name = None
    for seg in (route.segments or []):
        if not seg.polyline:
            continue
        text = f"{seg.name or ''} {seg.instruction or ''}"
        if any(w in text for w in ("收费站", "立交", "出口", "匝道")):
            gates.append(seg.polyline[0])
            gates.append(seg.polyline[-1])
        if controlled_segment(seg):
            if prev_ctrl_name and seg.name and seg.name != prev_ctrl_name:
                gates.append(seg.polyline[0])
            if seg.name:
                prev_ctrl_name = seg.name
    return gates


def _divergence_corridor_anchors(passenger_route, driver_routes, destination) -> list:
    if not driver_routes or len(passenger_route.polyline) < 2:
        return []
    gates = _highway_gate_points(passenger_route)
    probes = sample_path(passenger_route.polyline, 36)
    bridge_band = []
    transition_band = []
    split_pt = None
    for is_gate, pt in [*((True, p) for p in gates), *((False, p) for p in probes)]:
        gap = min((project_to_path(pt, dr.polyline)[2] for dr in driver_routes if dr.polyline), default=0.0)
        dist_c = haversine_m(pt, destination)
        gate_bonus = -2500.0 if is_gate else 0.0
        steiner_cost = 1.5 * gap + 1.0 * dist_c + gate_bonus
        if gap <= 800:
            if split_pt is None or dist_c < haversine_m(split_pt, destination):
                split_pt = pt
        elif 2800 <= gap <= 12000 and dist_c >= 4500:
            bridge_band.append((steiner_cost, pt))
        elif 800 < gap <= 12000 and dist_c >= 2500:
            transition_band.append((steiner_cost, pt))
    bridge_band.sort(key=lambda item: item[0])
    transition_band.sort(key=lambda item: item[0])
    anchors = []
    for _, pt in bridge_band + transition_band:
        if all(haversine_m(pt, existing) >= 2800 for existing in anchors):
            anchors.append(pt)
    if split_pt is not None and all(haversine_m(split_pt, existing) >= 2500 for existing in anchors):
        anchors.append(split_pt)
    return anchors


def recall_centers(routes, destination, limit=6, driver_routes=None, max_corridor_m=None):
    driver_list = driver_routes or []
    driver_ids = {id(r) for r in driver_list}

    # Spatio-Temporal Elliptical Pruning (Stiglic et al., 2015): focal points A (origin) and B (driver dest)
    _focal_a = next((r.polyline[0] for r in driver_list if r.polyline), None)
    _focal_b = next((r.polyline[-1] for r in driver_list if r.polyline), None)
    _ab_chord_m = haversine_m(_focal_a, _focal_b) if (_focal_a and _focal_b) else 0.0

    def within_corridor(pt):
        if not driver_list or max_corridor_m is None:
            return True
        if _focal_a and _focal_b and _ab_chord_m > 2000.0:
            # Elliptical major-axis excess bound: |AP| + |PB| - |AB| <= 2.2 * max_corridor_m
            if (haversine_m(_focal_a, pt) + haversine_m(pt, _focal_b) - _ab_chord_m) > max_corridor_m * 2.2:
                return False
        return min((project_to_path(pt, r.polyline)[2] for r in driver_list if r.polyline), default=0.0) <= max_corridor_m

    dest_near = within_corridor(destination)
    groups = [[destination]] if dest_near else []
    driver_groups, pax_groups = [], []
    for route in routes:
        exits = [a.place.point for a in original_exits(route)]
        samples = sample_path(route.polyline, 8)
        if driver_ids and id(route) not in driver_ids:
            div_anchors = _divergence_corridor_anchors(route, driver_list, destination)
            ordered_pts = [*div_anchors, *sorted(exits + samples, key=lambda p: haversine_m(p, destination))]
            filtered_pts = [p for p in ordered_pts if within_corridor(p)] or ordered_pts[:1]
            pax_groups.append(filtered_pts)
        else:
            q, _, _ = project_to_path(destination, route.polyline)
            ordered_pts = [q, *sorted(exits + samples, key=lambda p: haversine_m(p, destination))]
            filtered_pts = [p for p in ordered_pts if within_corridor(p)] or ordered_pts[:1]
            driver_groups.append(filtered_pts)
    if driver_groups:
        groups.append(driver_groups[0])
    if pax_groups:
        groups.append(pax_groups[0])
    groups.extend(driver_groups[1:])
    groups.extend(pax_groups[1:])
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

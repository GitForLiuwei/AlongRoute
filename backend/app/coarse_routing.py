"""Cheap priority estimates with separate hub and taxi coverage. Never a hard filter."""
import asyncio
import os

from .candidates import interleave_groups
from .geometry import haversine_m, point_along_path, project_to_path_with_kinematics
from .osrm import OsrmClient
from .preselection import candidate_key, candidate_trace
from .search_log import event

COARSE_TAXI_WEIGHTS = {
    'balanced': (1.5, 1.0, 108.0),
    'time':     (1.5, 1.5, 48.0),
    'cost':     (1.2, 0.8, 180.0),
}


def geometry_estimate(candidate, routes, destination, *, length_cache=None):
    # Owned by this estimate batch; never attach stale lengths to mutable Route models.
    if length_cache is None:
        length_cache = {}
    choices = []
    base = min(r.duration_s for r in routes)
    pt = candidate.place.point
    known_shared_s = getattr(candidate, 'approach_duration_s', None)
    for route in routes:
        _, progress, gap, seg_bearing, is_right = project_to_path_with_kinematics(pt, route.polyline)
        key = id(route)
        if key not in length_cache:
            length_cache[key] = sum(haversine_m(a, b) for a, b in zip(route.polyline, route.polyline[1:]))
        length = length_cache[key]
        route_speed = max(10.0, min(24.0, length / max(route.duration_s, 1)))
        street_speed = 32 / 3.6
        spike_detour = 2 * gap / street_speed
        blend = min(1.0, max(0.0, (gap - 1800.0) / 1200.0))
        local_detour = spike_detour
        if gap <= 1800:
            access_s = gap / street_speed
        else:
            # A local chord avoids treating a long curved route as one straight shortcut.
            window_m = max(1800.0, 2 * gap)
            a_pt = point_along_path(route.polyline, max(0.0, progress - window_m))
            b_pt = point_along_path(route.polyline, min(length, progress + window_m))
            tri_excess_m = max(0.0, haversine_m(a_pt, pt) + haversine_m(pt, b_pt) - haversine_m(a_pt, b_pt))
            branch_detour = tri_excess_m * 1.32 / route_speed + (1800.0 + 0.35 * (gap - 1800.0)) / street_speed
            local_detour = (1 - blend) * spike_detour + blend * min(spike_detour, branch_detour)
            access_s = ((1 - blend) * gap / street_speed
                        + blend * gap / max(route_speed * 0.75, street_speed))
        shared_est = route.duration_s * progress / max(length, 1) + access_s
        if known_shared_s is not None and known_shared_s > 0:
            rem_s = route.duration_s * max(0.0, length - progress) / max(length, 1) + gap / max(route_speed * 0.75, 32 / 3.6)
            detour = max(local_detour * 0.55, known_shared_s + rem_s - base)
            shared = known_shared_s
        else:
            detour = local_detour + max(0, route.duration_s - base)
            shared = shared_est
        choices.append((detour + shared, detour, shared, seg_bearing, is_right, gap))
    _, detour, shared, best_bearing, best_right, best_gap = min(choices)
    onward_m = haversine_m(pt, destination) * 1.35
    return dict(engine='geometry', shared_s=shared, detour_s=detour,
                onward_s=onward_m / (32 / 3.6), onward_m=onward_m,
                bearing_deg=best_bearing, right_side=best_right, gap_m=best_gap,
                constraint_aware=False, missing_reason='not_configured')


def _estimate_taxi_wait_s(candidate) -> float:
    """Estimate ride-hailing dispatch & pickup wait time (seconds) based on hub tier and location."""
    hub_kind = getattr(candidate, 'hub_kind', None) or getattr(candidate, 'source', '')
    name = getattr(getattr(candidate, 'place', None), 'name', '') or ''
    if hub_kind in {'subway', 'rail', 'hub'} or any(k in name for k in ('地铁', '站', '枢纽', '广场', '天河', '客运')):
        return 180.0
    if getattr(candidate, 'source', '') == 'exit' and not getattr(candidate, 'original_exit', True):
        return 420.0
    return 270.0


def _estimate_toll_friction_s(candidate) -> float:
    """Extra friction penalty for non-original highway exits and opposing-lane (left-hand side) roadside stops."""
    penalty = 0.0
    if getattr(candidate, 'exit_place', None) is not None and not getattr(candidate, 'original_exit', True):
        penalty += 150.0
    coarse = getattr(candidate, 'coarse', None) or {}
    gap_m = coarse.get('gap_m')
    if gap_m is not None and 5.0 < gap_m <= 180.0 and coarse.get('right_side') is False:
        penalty += 75.0
    return penalty


def taxi_priority(candidate, priority: str = 'balanced'):
    e = candidate.coarse
    w_driver, w_pax, sec_per_yuan = COARSE_TAXI_WEIGHTS.get(priority, COARSE_TAXI_WEIGHTS['balanced'])
    stop = 180 if candidate.source == 'parking' else 90
    toll_friction_s = _estimate_toll_friction_s(candidate)
    detour_total_s = (e.get('detour_s') or 0) + stop + toll_friction_s
    convex_excess_s = max(0.0, detour_total_s - 1080.0) * 0.75
    eff_detour_s = detour_total_s + convex_excess_s
    onward_m = e.get('onward_m') or 0
    wait_s = _estimate_taxi_wait_s(candidate)
    if 0 < onward_m <= 1080:
        if detour_total_s <= 900:
            walk_s = onward_m / 1.25
            return w_driver * eff_detour_s + w_pax * (e['shared_s'] + stop + walk_s)
        fare = 12 + max(0, onward_m / 1000 - 3) * 2.4
        return w_driver * eff_detour_s + w_pax * (e['shared_s'] + stop + e['onward_s'] + wait_s) + fare * sec_per_yuan
    fare = 12 + max(0, onward_m / 1000 - 3) * 2.4
    return w_driver * eff_detour_s + w_pax * (e['shared_s'] + stop + e['onward_s'] + wait_s) + fare * sec_per_yuan + 1800.0
    fare = 12 + max(0, onward_m / 1000 - 3) * 2.4
    return w_driver * eff_detour_s + w_pax * (e['shared_s'] + stop + e['onward_s'] + 300) + fare * sec_per_yuan


def transit_priority(candidate, destination):
    e = candidate.coarse
    dist_m = haversine_m(candidate.place.point, destination)
    if candidate.hub_kind == 'subway':
        onward_est_s = dist_m / 9.5 + 240
    elif candidate.hub_kind == 'bus':
        onward_est_s = dist_m / 4.2 + 540
    else:
        onward_est_s = dist_m / 10.0 + 360
    penalty = 1800.0 if (0 < dist_m < 1200 and (e.get('detour_s') or 0) > 900) else 0.0
    return e['detour_s'] + e['shared_s'] + onward_est_s + penalty


def _interleave_coverage(candidates, destination, modes, priority: str = 'balanced'):
    groups = {}
    for candidate in candidates:
        kind = candidate.hub_kind or candidate.source
        groups.setdefault((candidate.route_index, candidate.region, kind), []).append(candidate)
    taxi, transit_subway, transit_other = [], [], []
    for key in sorted(groups):
        group = groups[key]
        taxi_elig = [c for c in group if 'taxi' in (c.modes or modes)]
        if taxi_elig:
            taxi.append(sorted(taxi_elig, key=lambda c: (taxi_priority(c, priority), candidate_key(c))))
        if 'transit' in modes and any(c.hub_kind in ('subway', 'bus') or c.source == 'station_area' for c in group):
            sorted_grp = sorted(group, key=lambda c: (transit_priority(c, destination), candidate_key(c)))
            if any(c.hub_kind == 'subway' for c in group):
                transit_subway.append(sorted_grp)
            else:
                transit_other.append(sorted_grp)
    taxi.sort(key=lambda g: (taxi_priority(g[0], priority), candidate_key(g[0])))
    transit_subway.sort(key=lambda g: (transit_priority(g[0], destination), candidate_key(g[0])))
    transit_other.sort(key=lambda g: (transit_priority(g[0], destination), candidate_key(g[0])))
    transit_combined = interleave_groups(transit_subway) + interleave_groups(transit_other)
    ordered, seen = [], set()
    for c in interleave_groups([transit_combined, interleave_groups(taxi)]):
        key = candidate_key(c)
        if key not in seen:
            ordered.append(c)
            seen.add(key)
    for c in candidates:
        key = candidate_key(c)
        if key not in seen:
            ordered.append(c)
            seen.add(key)
    return ordered


def coverage_order(candidates, destination, modes, max_detour_s: float | None = None, priority: str = 'balanced'):
    if max_detour_s is None:
        return _interleave_coverage(candidates, destination, modes, priority=priority)
    soft_limit_s = max_detour_s * 1.5 + 180
    feasible, overflow = [], []
    for c in candidates:
        stop_s = 180 if c.source == 'parking' else 90
        detour_s = (c.coarse.get('detour_s') or 0) + stop_s
        if detour_s <= soft_limit_s:
            feasible.append(c)
        else:
            overflow.append(c)
    if not feasible or not overflow:
        return _interleave_coverage(candidates, destination, modes, priority=priority)
    return (
        _interleave_coverage(feasible, destination, modes, priority=priority)
        + _interleave_coverage(overflow, destination, modes, priority=priority)
    )


async def estimate_candidates(api, request, routes, candidates):
    length_cache = {}
    for candidate in candidates:
        candidate.coarse = geometry_estimate(candidate, routes, request.passenger_destination.point,
                                             length_cache=length_cache)
    url = os.getenv('OSRM_BASE_URL', '').strip()
    if url and candidates:
        try:
            _cand_bearings = [
                (c.coarse or {}).get('bearing_deg') if ((c.coarse or {}).get('gap_m') or 999) <= 120 else None
                for c in candidates
            ]
            estimates = await asyncio.wait_for(OsrmClient(api.client, url).estimate(
                request.origin.point, request.driver_destination.point, request.passenger_destination.point,
                [c.place.point for c in candidates], bearings=_cand_bearings), timeout=6)
        except asyncio.TimeoutError:
            estimates = []
        for i, candidate in enumerate(candidates):
            e = estimates[i] if i < len(estimates) else None
            if e is None or e.missing_reason:
                candidate.coarse['missing_reason'] = e.missing_reason if e else 'matrix_timeout'
            else:
                _prev_c = candidate.coarse or {}
                candidate.coarse = dict(engine='osrm', shared_s=e.shared_s, detour_s=e.detour_s,
                                        onward_s=e.onward_s, onward_m=e.onward_m, constraint_aware=False,
                                        bearing_deg=_prev_c.get('bearing_deg'), right_side=_prev_c.get('right_side'),
                                        gap_m=_prev_c.get('gap_m'),
                                        missing_reason=None, network_version=os.getenv('OSRM_DATA_VERSION', 'unspecified'))
    priority = getattr(request.preferences, 'priority', 'balanced') or 'balanced'
    for candidate in candidates:
        candidate.estimate_s = taxi_priority(candidate, priority)
        event('candidate_coarse', point=candidate.place.model_dump(mode='json'),
              estimate=candidate.coarse, **candidate_trace(candidate))
    max_detour_s = request.preferences.max_detour_minutes * 60
    return coverage_order(candidates, request.passenger_destination.point,
                          request.preferences.modes, max_detour_s=max_detour_s, priority=priority)

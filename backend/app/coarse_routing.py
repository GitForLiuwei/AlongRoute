"""Cheap priority estimates with separate hub and taxi coverage. Never a hard filter."""
import asyncio
import os

from .candidates import interleave_groups
from .geometry import haversine_m, project_to_path
from .osrm import OsrmClient
from .preselection import candidate_key, candidate_trace
from .search_log import event

COARSE_TAXI_WEIGHTS = {
    'balanced': (1.5, 1.0, 108.0),
    'time':     (1.5, 1.5, 48.0),
    'cost':     (1.2, 0.8, 180.0),
}


def geometry_estimate(candidate, routes, destination):
    choices = []
    base = min(r.duration_s for r in routes)
    pt = candidate.place.point
    known_shared_s = getattr(candidate, 'approach_duration_s', None)
    for route in routes:
        _, progress, gap = project_to_path(pt, route.polyline)
        length = sum(haversine_m(a, b) for a, b in zip(route.polyline, route.polyline[1:]))
        route_speed = max(10.0, min(24.0, length / max(route.duration_s, 1)))
        if gap <= 1800:
            local_detour = 2 * gap / (32 / 3.6)
            shared_est = route.duration_s * progress / max(length, 1) + gap / (32 / 3.6)
        else:
            a_pt, b_pt = route.polyline[0], route.polyline[-1]
            tri_excess_m = max(0.0, haversine_m(a_pt, pt) + haversine_m(pt, b_pt) - haversine_m(a_pt, b_pt))
            branch_detour = (tri_excess_m * 1.35) / route_speed + min(1800.0, gap) / (32 / 3.6)
            spike_detour = 2 * gap / (32 / 3.6)
            local_detour = min(spike_detour, branch_detour)
            shared_est = route.duration_s * progress / max(length, 1) + gap / max(route_speed * 0.75, 32 / 3.6)
        if known_shared_s is not None and known_shared_s > 0:
            rem_s = route.duration_s * max(0.0, length - progress) / max(length, 1) + gap / max(route_speed * 0.75, 32 / 3.6)
            detour = max(local_detour * 0.55, known_shared_s + rem_s - base)
            shared = known_shared_s
        else:
            detour = local_detour + max(0, route.duration_s - base)
            shared = shared_est
        choices.append((detour + shared, detour, shared))
    _, detour, shared = min(choices)
    onward_m = haversine_m(pt, destination) * 1.35
    return dict(engine='geometry', shared_s=shared, detour_s=detour,
                onward_s=onward_m / (32 / 3.6), onward_m=onward_m,
                constraint_aware=False, missing_reason='not_configured')


def taxi_priority(candidate, priority: str = 'balanced'):
    e = candidate.coarse
    w_driver, w_pax, sec_per_yuan = COARSE_TAXI_WEIGHTS.get(priority, COARSE_TAXI_WEIGHTS['balanced'])
    stop = 180 if candidate.source == 'parking' else 90
    detour_total_s = (e.get('detour_s') or 0) + stop
    convex_excess_s = max(0.0, detour_total_s - 1080.0) * 0.75
    eff_detour_s = detour_total_s + convex_excess_s
    onward_m = e.get('onward_m') or 0
    if 0 < onward_m <= 1080:
        if detour_total_s <= 900:
            walk_s = onward_m / 1.25
            return w_driver * eff_detour_s + w_pax * (e['shared_s'] + stop + walk_s)
        fare = 12 + max(0, onward_m / 1000 - 3) * 2.4
        return w_driver * eff_detour_s + w_pax * (e['shared_s'] + stop + e['onward_s'] + 300) + fare * sec_per_yuan + 1800.0
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
    for candidate in candidates:
        candidate.coarse = geometry_estimate(candidate, routes, request.passenger_destination.point)
    url = os.getenv('OSRM_BASE_URL', '').strip()
    if url and candidates:
        try:
            estimates = await asyncio.wait_for(OsrmClient(api.client, url).estimate(
                request.origin.point, request.driver_destination.point, request.passenger_destination.point,
                [c.place.point for c in candidates]), timeout=6)
        except asyncio.TimeoutError:
            estimates = []
        for i, candidate in enumerate(candidates):
            e = estimates[i] if i < len(estimates) else None
            if e is None or e.missing_reason:
                candidate.coarse['missing_reason'] = e.missing_reason if e else 'matrix_timeout'
            else:
                candidate.coarse = dict(engine='osrm', shared_s=e.shared_s, detour_s=e.detour_s,
                                        onward_s=e.onward_s, onward_m=e.onward_m, constraint_aware=False,
                                        missing_reason=None, network_version=os.getenv('OSRM_DATA_VERSION', 'unspecified'))
    priority = getattr(request.preferences, 'priority', 'balanced') or 'balanced'
    for candidate in candidates:
        candidate.estimate_s = taxi_priority(candidate, priority)
        event('candidate_coarse', point=candidate.place.model_dump(mode='json'),
              estimate=candidate.coarse, **candidate_trace(candidate))
    max_detour_s = request.preferences.max_detour_minutes * 60
    return coverage_order(candidates, request.passenger_destination.point,
                          request.preferences.modes, max_detour_s=max_detour_s, priority=priority)

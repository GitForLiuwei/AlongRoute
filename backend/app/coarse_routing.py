"""Cheap priority estimates with separate hub and taxi coverage. Never a hard filter."""
import asyncio
import os

from .candidates import interleave_groups
from .geometry import haversine_m, project_to_path
from .osrm import OsrmClient
from .preselection import candidate_key, candidate_trace
from .search_log import event


def geometry_estimate(candidate, routes, destination):
    choices = []
    base = min(r.duration_s for r in routes)
    for route in routes:
        _, progress, gap = project_to_path(candidate.place.point, route.polyline)
        length = sum(haversine_m(a, b) for a, b in zip(route.polyline, route.polyline[1:]))
        detour = 2 * gap / (32 / 3.6) + max(0, route.duration_s - base)
        shared = route.duration_s * progress / max(length, 1) + gap / (32 / 3.6)
        choices.append((detour + shared, detour, shared))
    _, detour, shared = min(choices)
    onward_m = haversine_m(candidate.place.point, destination) * 1.35
    return dict(engine='geometry', shared_s=shared, detour_s=detour,
                onward_s=onward_m / (32 / 3.6), onward_m=onward_m,
                constraint_aware=False, missing_reason='not_configured')


def taxi_priority(candidate):
    e = candidate.coarse
    # Internal estimate only; never returned as a quoted fare or used for budgets.
    fare = 12 + max(0, e['onward_m'] / 1000 - 3) * 2.4
    stop = 180 if candidate.source == 'parking' else 90
    return e['detour_s'] + e['shared_s'] + 2 * stop + e['onward_s'] + 300 + fare * 180


def transit_priority(candidate, destination):
    e = candidate.coarse
    # Road travel to C is not a proxy for rail travel. Keep independent hub coverage.
    return e['detour_s'] + e['shared_s'] + haversine_m(candidate.place.point, destination) / 15


def coverage_order(candidates, destination, modes):
    groups = {}
    for candidate in candidates:
        kind = candidate.hub_kind or candidate.source
        groups.setdefault((candidate.route_index, candidate.region, kind), []).append(candidate)
    taxi, transit = [], []
    for key in sorted(groups):
        group = groups[key]
        taxi.append(sorted(group, key=lambda c: (taxi_priority(c), candidate_key(c))))
        if 'transit' in modes and any(c.hub_kind in ('subway', 'bus') or c.source == 'station_area' for c in group):
            transit.append(sorted(group, key=lambda c: (transit_priority(c, destination), candidate_key(c))))
    # Rank groups by their best point, but take one from every group before its second.
    taxi.sort(key=lambda g: (taxi_priority(g[0]), candidate_key(g[0])))
    transit.sort(key=lambda g: (transit_priority(g[0], destination), candidate_key(g[0])))
    ordered, seen = [], set()
    for c in interleave_groups([interleave_groups(transit), interleave_groups(taxi)]):
        key = candidate_key(c)
        if key not in seen:
            ordered.append(c)
            seen.add(key)
    return ordered


async def estimate_candidates(api, request, routes, candidates):
    for candidate in candidates:
        candidate.coarse = geometry_estimate(candidate, routes, request.passenger_destination.point)
    url = os.getenv('OSRM_BASE_URL', '').strip()
    if url and candidates:
        try:
            estimates = await asyncio.wait_for(OsrmClient(api.client, url).estimate(
                request.origin.point, request.driver_destination.point, request.passenger_destination.point,
                [c.place.point for c in candidates]), timeout=4)
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
    for candidate in candidates:
        candidate.estimate_s = taxi_priority(candidate)
        event('candidate_coarse', point=candidate.place.model_dump(mode='json'),
              estimate=candidate.coarse, **candidate_trace(candidate))
    return coverage_order(candidates, request.passenger_destination.point, request.preferences.modes)

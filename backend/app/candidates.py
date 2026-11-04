"""Bounded, spatially diverse candidates; coordinates are never parking permission."""
from dataclasses import dataclass
import math

from .geometry import haversine_m, point_along_path, project_to_path, sample_path
from .models import Place, Point, RecommendationRequest, Route


@dataclass
class Station:
    place: Place
    kind: str


@dataclass
class Candidate:
    place: Place
    source: str
    progress_m: float
    station_distance_m: float | None
    estimate_s: float
    exit_place: Place | None = None
    approach: Point | None = None
    original_exit: bool = False
    modes: tuple[str, ...] = ("taxi", "transit")


def restricted_road(name: str) -> bool:
    if any(word in name for word in ("匝道", "隧道", "高架", "立交", "大桥", "收费站")):
        return True
    return "辅路" not in name and any(word in name for word in ("高速", "快速"))


def search_centers(route: Route) -> list[Point]:
    if not route.segments:
        return sample_path(route.polyline, count=min(60, max(6, math.ceil(route.distance_m / 1200))))
    centers = []
    for segment in route.segments:
        if not segment.polyline or restricted_segment(segment):
            continue
        length = sum(haversine_m(a, b) for a, b in zip(segment.polyline, segment.polyline[1:]))
        for point in [segment.polyline[0], *sample_path(segment.polyline, count=max(1, math.ceil(length / 1200)))]:
            if not any(haversine_m(point, other) < 500 for other in centers):
                centers.append(point)
    return centers[:60]


def controlled_segment(segment) -> bool:
    if "辅路" in segment.name:
        return False
    return any(word in segment.name + segment.instruction for word in ("高速", "快速"))


def restricted_segment(segment) -> bool:
    return restricted_road(segment.name) or controlled_segment(segment)


def terminal_is_usable(route: Route, point: Point, at_end: bool) -> bool:
    """Reject known fast-road/bridge/tunnel endpoints, and large routing snaps."""
    if route.is_schematic or not route.polyline or not any(segment.name for segment in route.segments):
        return False
    endpoint = route.polyline[-1] if at_end else route.polyline[0]
    if haversine_m(endpoint, point) > 30:
        return False
    known_nearby = False
    segments = reversed(route.segments) if at_end else route.segments
    for segment in segments:
        if not segment.polyline:
            continue
        _, _, distance = project_to_path(point, segment.polyline)
        if distance <= 5 and segment.name:
            known_nearby = True
        if distance <= 50 and restricted_segment(segment):
            return False
    return known_nearby


def build_candidates(route: Route, parking: list[Place], stations: list[Station],
                     request: RecommendationRequest, limit=36) -> list[Candidate]:
    path = route.polyline
    if len(path) < 2:
        return []
    path_length = sum(haversine_m(a, b) for a, b in zip(path, path[1:]))
    raw: list[tuple[Place, str]] = [(place, "parking") for place in parking]
    # Real road geometry supplies points independent of parking POI availability.
    if not route.is_schematic:
        seeds = sample_path(path, count=min(100, max(12, math.ceil(path_length / 200))))
        seeds = [path[0], *seeds, path[-1]]
        for point in seeds:
            raw.append((Place(name="沿途道路附近候选", point=point,
                              citycode=request.origin.citycode), "roadside"))
        for station in stations if "transit" in request.preferences.modes else []:
            _, progress, distance = project_to_path(station.place.point, path)
            if distance > 1000:
                continue
            # A bus platform is an anchor, never the car's stopping coordinate.
            for offset in (-100, -50, 50, 100):
                point = point_along_path(path, progress + offset)
                raw.append((Place(name=f"{station.place.name}附近道路"[:120], point=point,
                                  citycode=station.place.citycode or request.origin.citycode), "station_area"))

    junctions = [segment.polyline[-1] for segment in route.segments
                 if segment.polyline and any(word in segment.end_action for word in ("左转", "右转", "掉头", "环岛"))]
    restricted = [segment for segment in route.segments if restricted_segment(segment)]
    candidates = []
    for place, source in raw:
        if restricted_road(place.name):
            continue
        if any(haversine_m(place.point, station.place.point) <= 30
               for station in stations if station.kind == "bus"):
            continue
        if any(haversine_m(place.point, junction) <= 50 for junction in junctions):
            continue
        if any(segment.polyline and project_to_path(place.point, segment.polyline)[2] <= 25
               for segment in restricted):
            continue
        _, progress, lateral = project_to_path(place.point, path)
        station_distance = min((haversine_m(place.point, station.place.point)
                                for station in stations), default=None)
        # Only a coarse pruning bound. Actual access is checked against the
        # station/entrance used by the returned transit itinerary on walking roads.
        estimate = route.duration_s * progress / max(path_length, 1) + haversine_m(
            place.point, request.passenger_destination.point) / 10 + lateral / 5
        candidates.append(Candidate(place, source, progress, station_distance, estimate))

    # Reserve coverage along the route; do not cut every candidate solely by its
    # distance to the driver's route. Include station access in the coarse score.
    def score(candidate):
        station_penalty = 0
        if "transit" in request.preferences.modes:
            station_penalty = min(candidate.station_distance_m if candidate.station_distance_m is not None else 1000, 1000)
        return candidate.estimate_s + station_penalty / 1.2

    ordered = sorted(candidates, key=score)
    selected: list[Candidate] = []
    buckets = {index: 0 for index in range(6)}

    def add(candidate):
        # Proximity alone cannot establish equivalence across road sides/levels.
        if any(candidate.place.point.csv() == item.place.point.csv() for item in selected):
            return False
        selected.append(candidate)
        bucket = min(5, int(candidate.progress_m / max(path_length, 1) * 6))
        buckets[bucket] += 1
        return True

    # Round-robin across route sections before filling by heuristic score.
    for _ in range(max(1, limit // 6)):
        for bucket in range(6):
            for candidate in ordered:
                candidate_bucket = min(5, int(candidate.progress_m / max(path_length, 1) * 6))
                if candidate_bucket == bucket and buckets[bucket] <= _ and add(candidate):
                    break
            if len(selected) >= limit:
                return selected
    for candidate in ordered:
        if len(selected) >= limit:
            break
        add(candidate)
    return selected

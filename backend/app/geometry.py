import math
from .models import Point


def haversine_m(a: Point, b: Point) -> float:
    radius = 6371000
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dl = lat2 - lat1
    dn = math.radians(b.lon - a.lon)
    h = math.sin(dl / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dn / 2) ** 2
    return 2 * radius * math.asin(min(1, math.sqrt(h)))


def interpolate(a: Point, b: Point, ratio: float) -> Point:
    return Point(lon=a.lon + (b.lon - a.lon) * ratio, lat=a.lat + (b.lat - a.lat) * ratio)


def project_to_path(point: Point, path: list[Point]) -> tuple[Point, float, float]:
    """Return nearest projected point, distance along route, and lateral distance."""
    if not path:
        raise ValueError("Empty path")
    best, progress, lateral = path[0], 0.0, haversine_m(point, path[0])
    walked = 0.0
    scale = math.cos(math.radians(point.lat))
    for a, b in zip(path, path[1:]):
        dx, dy = (b.lon - a.lon) * scale, b.lat - a.lat
        px, py = (point.lon - a.lon) * scale, point.lat - a.lat
        ratio = max(0, min(1, (px * dx + py * dy) / max(dx * dx + dy * dy, 1e-18)))
        projected = interpolate(a, b, ratio)
        distance, length = haversine_m(point, projected), haversine_m(a, b)
        if distance < lateral:
            best, progress, lateral = projected, walked + ratio * length, distance
        walked += length
    return best, progress, lateral


def point_along_path(path: list[Point], distance: float) -> Point:
    remaining = max(0, distance)
    for a, b in zip(path, path[1:]):
        length = haversine_m(a, b)
        if remaining <= length:
            return interpolate(a, b, remaining / max(length, 1e-9))
        remaining -= length
    return path[-1]


def sample_path(polyline: list[Point], count: int = 8) -> list[Point]:
    """Sample by approximate walked/geodesic distance along polyline, not by vertex index."""
    if len(polyline) < 2:
        return polyline[:]
    lengths = [haversine_m(a, b) for a, b in zip(polyline, polyline[1:])]
    total = sum(lengths)
    if total < 1:
        return [polyline[0]]
    result = []
    for k in range(1, count + 1):
        target = total * k / (count + 1)
        walked = 0.0
        for i, distance in enumerate(lengths):
            if walked + distance >= target:
                result.append(interpolate(polyline[i], polyline[i + 1], (target - walked) / max(distance, 1e-9)))
                break
            walked += distance
    return result


def bearing_deg(a: Point, b: Point) -> float:
    """Compute initial compass bearing in degrees [0, 360) from point a to point b."""
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlon = math.radians(b.lon - a.lon)
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def project_to_path_with_kinematics(point: Point, path: list[Point]) -> tuple[Point, float, float, float, bool]:
    """Return (projected_point, progress_m, lateral_m, tangent_bearing_deg, is_right_hand_side).

    In right-hand traffic (China mainland), a drop-off point on the right side of the driver's
    forward tangent vector avoids crossing opposing lanes or making U-turns.
    """
    if not path:
        raise ValueError("Empty path")
    if len(path) == 1:
        return path[0], 0.0, haversine_m(point, path[0]), 0.0, True

    best = path[0]
    progress = 0.0
    lateral = haversine_m(point, path[0])
    best_bearing = bearing_deg(path[0], path[1])
    best_right_side = True
    walked = 0.0
    scale = math.cos(math.radians(point.lat))

    for a, b in zip(path, path[1:]):
        dx, dy = (b.lon - a.lon) * scale, b.lat - a.lat
        px, py = (point.lon - a.lon) * scale, point.lat - a.lat
        denom = dx * dx + dy * dy
        ratio = max(0.0, min(1.0, (px * dx + py * dy) / max(denom, 1e-18)))
        projected = interpolate(a, b, ratio)
        distance = haversine_m(point, projected)
        length = haversine_m(a, b)
        if distance < lateral:
            best, progress, lateral = projected, walked + ratio * length, distance
            if denom > 1e-16:
                best_bearing = bearing_deg(a, b)
                # 2D cross product (tangent x offset): dx * py - dy * px < 0 means right-hand side of travel
                cross_z = dx * py - dy * px
                best_right_side = bool(cross_z <= 0.0)
        walked += length

    return best, progress, lateral, best_bearing, best_right_side

"""Exit discovery anchors; a map exit is never itself a drop-off position."""
from dataclasses import dataclass

from .candidates import controlled_segment, restricted_segment
from .geometry import haversine_m, point_along_path, project_to_path, sample_path
from .models import Place, Point, Route


@dataclass
class ExitAnchor:
    place: Place
    approach: Point
    progress_m: float
    original: bool = False


CLOSED_ROAD_KEYWORDS = (
    "高速", "收费站", "高架", "快速路", "快速干道", "内环", "中环", "外环",
    "三环主路", "四环主路", "五环主路", "隧道", "大桥", "立交", "枢纽",
)
SURFACE_ROAD_EXCLUSIONS = ("辅路", "地面", "辅道", "桥下")


def _is_closed_motorway(segment) -> bool:
    name = segment.name or ""
    return (not any(word in name for word in SURFACE_ROAD_EXCLUSIONS)
            and any(word in name for word in CLOSED_ROAD_KEYWORDS))


def highway_centers(route: Route) -> list[Point]:
    points = []
    for segment in route.segments:
        if not controlled_segment(segment) or not segment.polyline:
            continue
        length = sum(haversine_m(a, b) for a, b in zip(segment.polyline, segment.polyline[1:]))
        for point in [segment.polyline[0], *sample_path(segment.polyline, count=min(30, max(1, int(length / 1200))))]:
            if not any(haversine_m(point, other) < 800 for other in points):
                points.append(point)
    return points if len(points) <= 40 else [points[round(index * (len(points) - 1) / 39)] for index in range(40)]


def original_exits(route: Route, citycode=None) -> list[ExitAnchor]:
    anchors = []
    last_highway = None
    for segment in route.segments:
        if not segment.polyline:
            continue
        if controlled_segment(segment):
            # Keep following controlled urban expressways; only record a transition
            # when the next segment is outside the recognized closed-road classes.
            if (last_highway is not None
                    and _is_closed_motorway(last_highway)
                    and not _is_closed_motorway(segment)
                    and segment.name):
                point = segment.polyline[0]
                _, progress, _ = project_to_path(point, route.polyline)
                seg_len = sum(haversine_m(a, b) for a, b in zip(last_highway.polyline, last_highway.polyline[1:]))
                approach = point_along_path(last_highway.polyline, max(0, seg_len - 300))
                anchors.append(ExitAnchor(
                    Place(name=f"原路线驶出后: {segment.name}"[:120], point=point, citycode=citycode),
                    approach, progress, True))
            last_highway = segment
        elif last_highway is not None and not restricted_segment(segment) and segment.name:
            # Only a transition onto an ordinary road; highway-to-highway interchanges do not count.
            point = segment.polyline[0]
            _, progress, _ = project_to_path(point, route.polyline)
            approach = point_along_path(last_highway.polyline, max(0, sum(haversine_m(a, b) for a, b in zip(last_highway.polyline, last_highway.polyline[1:])) - 300))
            anchors.append(ExitAnchor(Place(name=f"原路线驶出后：{segment.name}"[:120], point=point, citycode=citycode), approach, progress, True))
            last_highway = None
    return anchors


def discovered_exit(route: Route, place: Place) -> ExitAnchor | None:
    if not any(word in place.name for word in ("出口", "收费站")) or any(word in place.name for word in ("入口", "服务区")):
        return None
    # Search only against controlled-access segments, not a nearby ordinary road.
    possible = []
    for segment in route.segments:
        if not controlled_segment(segment) or len(segment.polyline) < 2:
            continue
        projected, _, gap = project_to_path(place.point, segment.polyline)
        _, progress, _ = project_to_path(projected, route.polyline)
        if gap <= 1800 and progress > 100:
            possible.append((gap, progress))
    if not possible:
        return None
    _, progress = min(possible)
    return ExitAnchor(place, point_along_path(route.polyline, max(0, progress - 300)), progress)


def exit_was_used(route: Route, anchor: ExitAnchor, dropoff: Point) -> bool:
    # The full provider route must encounter approach -> exit -> dropoff, in this order.
    _, approach_progress, approach_gap = project_to_path(anchor.approach, route.polyline)
    _, exit_progress, exit_gap = project_to_path(anchor.place.point, route.polyline)
    _, dropoff_progress, dropoff_gap = project_to_path(dropoff, route.polyline)
    return (approach_gap <= 30 and exit_gap <= 50 and dropoff_gap <= 5
            and approach_progress < exit_progress < dropoff_progress)

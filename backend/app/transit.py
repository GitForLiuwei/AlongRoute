"""Parse each provider itinerary before applying constraints or preferences."""
import math
import re
from .models import ModeOption, Place, Point, TransitStep


def numeric(value):
    try:
        parsed = float(value) if value is not None and value != "" else None
        return parsed if parsed is not None and math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def coordinates(value) -> list[Point]:
    while isinstance(value, dict):
        value = value.get("polyline")
    if not isinstance(value, str):
        return []
    result = []
    for item in value.split(";"):
        try:
            lon, lat = map(float, item.split(","))
            result.append(Point(lon=lon, lat=lat))
        except (TypeError, ValueError):
            continue
    return result


def text(value) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    return None


def nonnegative_int(value) -> int | None:
    number = numeric(value)
    return round(number) if number is not None and number >= 0 else None


def transport_kind(bus: dict) -> str | None:
    value = str(bus.get("type") or "").lower()
    if not value:
        return None
    return "subway" if any(word in value for word in ("地铁", "轻轨", "subway", "metro")) else "bus"


def line_direction(bus: dict) -> str | None:
    end = bus.get("end_stop")
    if isinstance(end, dict):
        end = end.get("name")
    if text(end):
        return text(end)
    match = re.search(r"[（(]([^（）()]+)[）)]$", str(bus.get("name") or ""))
    if match:
        parts = re.split(r"--|→|—|至|-", match.group(1))
        if len(parts) > 1:
            return text(parts[-1])
    return None


def line_label(bus: dict) -> str:
    name = str(bus.get("name") or "公共交通")
    match = re.search(r"\s*[（(]([^（）()]+)[）)]$", name)
    if match and re.search(r"--|→|—|至|-", match.group(1)):
        return name[:match.start()].strip()
    return name


def parse_itinerary(item: dict, citycode: str, destination_name: str = "目的地") -> ModeOption | None:
    cost = item.get("cost") or {}
    duration = numeric(cost.get("duration"))
    if duration is None or duration < 0:
        return None
    walking = numeric(item.get("walking_distance"))
    measured_walks = []
    coords, lines = [], []
    first_stop = None
    kind = None
    vehicles = 0
    access_m = access_s = None
    steps = []
    previous_stop = "下车点"
    for segment in item.get("segments") or []:
        if any(segment.get(key) for key in ("railway", "taxi")):
            return None  # Unsupported legs must never silently disappear.
        walk = segment.get("walking") or {}
        if walk:
            measured_walks.append(numeric(walk.get("distance")))
        for step in walk.get("steps") or []:
            coords.extend(coordinates(step.get("polyline")))
        buses = (segment.get("bus") or {}).get("buslines") or []
        bus = buses[0] if buses else None
        departure = (bus.get("departure_stop") or {}) if bus else {}
        walk_distance = nonnegative_int(walk.get("distance"))
        walk_duration = nonnegative_int((walk.get("cost") or {}).get("duration", walk.get("duration")))
        instructions = [value for step in walk.get("steps") or [] if (value := text(step.get("instruction")))]
        if walk and (walk_distance != 0 or instructions or vehicles == 0):
            target = text(departure.get("name")) or ("上车站待确认" if bus else destination_name)
            entrance = text((departure.get("entrance") or {}).get("name"))
            if entrance:
                target += entrance
            steps.append(TransitStep(kind="walk", title="步行到站" if vehicles == 0 else ("步行换乘" if bus else "步行到目的地"),
                distance_m=walk_distance, duration_s=walk_duration, from_stop=previous_stop, to_stop=target,
                instructions=instructions))
        if not buses:
            continue
        ride_kind = transport_kind(bus)
        if ride_kind is None:
            return None
        arrival = bus.get("arrival_stop") or {}
        via_stops = [name for stop in bus.get("via_stops") or [] if (name := text(stop.get("name")))]
        via_num = nonnegative_int(bus.get("via_num"))
        count = via_num + 1 if via_num is not None else (len(via_stops) + 1 if via_stops else None)
        steps.append(TransitStep(kind=ride_kind, title="乘车" if vehicles == 0 else "换乘",
            line_name=line_label(bus), direction=line_direction(bus),
            from_stop=text(departure.get("name")), to_stop=text(arrival.get("name")), stop_count=count,
            via_stops=via_stops, distance_m=nonnegative_int(bus.get("distance")),
            duration_s=nonnegative_int((bus.get("cost") or {}).get("duration", bus.get("duration"))),
            entrance=text((departure.get("entrance") or {}).get("name")),
            exit=text((arrival.get("exit") or {}).get("name"))))
        previous_stop = text(arrival.get("name")) or "下车站待确认"
        vehicles += 1
        lines.append(str(bus.get("name") or "公共交通"))
        coords.extend(coordinates(bus.get("polyline")))
        if first_stop is None:
            access_m = numeric(walk.get("distance"))
            access_s = numeric((walk.get("cost") or {}).get("duration"))
            if access_s is None:
                access_s = numeric(walk.get("duration"))
            departure = bus.get("departure_stop") or {}
            kind = ride_kind
            target = (departure.get("entrance") or {}) if kind == "subway" else departure
            points = coordinates(target.get("location"))
            if not points:
                return None  # Underground station centers cannot stand in for entrances.
            first_stop = Place(name=(str(departure.get("name") or "上车站") +
                                     (str(target.get("name") or "入口") if kind == "subway" else ""))[:120],
                               point=points[0], citycode=citycode,
                               poi_id=str(departure.get("id") or "") or None)
    if not first_stop or not vehicles:
        return None  # A walking-only result is not a public transport itinerary.
    if walking is None:
        if not measured_walks or any(distance is None for distance in measured_walks):
            return None
        walking = sum(measured_walks)
    if walking < 0:
        return None
    return ModeOption(mode="transit", duration_s=round(duration),
                      fare_yuan=numeric(cost.get("transit_fee")), walking_m=round(walking),
                      transfers=vehicles - 1, details=" → ".join(lines), route=coords,
                      boarding_stop=first_stop, boarding_kind=kind,
                      transit_steps=steps,
                      original_access_m=round(access_m) if access_m is not None else None,
                      original_access_s=round(access_s) if access_s is not None else None,
                      notes=["按预计到站时间查询；发车和换乘以实际运营为准"])

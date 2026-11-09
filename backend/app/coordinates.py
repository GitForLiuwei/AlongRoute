"""Coordinate conversion only at the OSM boundary; business points remain GCJ-02."""
import math

from .models import Point


def wgs84_to_gcj02(point: Point) -> Point:
    lon, lat = point.lon, point.lat
    if not (72.004 <= lon <= 137.8347 and .8293 <= lat <= 55.8271):
        return point.model_copy()
    x, y = lon - 105, lat - 35
    wave = (20 * math.sin(6 * x * math.pi) + 20 * math.sin(2 * x * math.pi)) * 2 / 3
    dy = -100 + 2*x + 3*y + .2*y*y + .1*x*y + .2*math.sqrt(abs(x)) + wave
    dy += (20*math.sin(y*math.pi) + 40*math.sin(y*math.pi/3)) * 2/3
    dy += (160*math.sin(y*math.pi/12) + 320*math.sin(y*math.pi/30)) * 2/3
    dx = 300 + x + 2*y + .1*x*x + .1*x*y + .1*math.sqrt(abs(x)) + wave
    dx += (20*math.sin(x*math.pi) + 40*math.sin(x*math.pi/3)) * 2/3
    dx += (150*math.sin(x*math.pi/12) + 300*math.sin(x*math.pi/30)) * 2/3
    rad = math.radians(lat)
    magic = 1 - .00669342162296594323 * math.sin(rad)**2
    dy = dy * 180 / ((6378245 * (1 - .00669342162296594323) / magic**1.5) * math.pi)
    dx = dx * 180 / ((6378245 / math.sqrt(magic)) * math.cos(rad) * math.pi)
    return Point(lon=lon + dx, lat=lat + dy)


def gcj02_to_wgs84(point: Point) -> Point:
    # Iterative inverse of the approximation, not a road-side/level guarantee.
    estimate = point.model_copy()
    for _ in range(5):
        projected = wgs84_to_gcj02(estimate)
        estimate = Point(lon=estimate.lon + point.lon - projected.lon,
                         lat=estimate.lat + point.lat - projected.lat)
    return estimate

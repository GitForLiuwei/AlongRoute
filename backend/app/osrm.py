"""Optional local, directed matrix estimates. Missing cells never mean infeasible."""
import asyncio
from dataclasses import dataclass
import math

import httpx

from .coordinates import gcj02_to_wgs84
from .geometry import haversine_m
from .models import Point


@dataclass
class MatrixEstimate:
    shared_s: float | None = None
    detour_s: float | None = None
    onward_s: float | None = None
    onward_m: float | None = None
    missing_reason: str | None = None


def nonnegative(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
        return float(value)
    return None


class OsrmClient:
    def __init__(self, client: httpx.AsyncClient, url: str, timeout=1.5, batch_size=80):
        self.client, self.url, self.timeout = client, url.rstrip('/'), timeout
        self.batch_size = max(1, min(batch_size, 80))  # 83 locations incl. A/B/C.

    async def estimate(self, a: Point, b: Point, c: Point, points: list[Point]):
        result = []
        for start in range(0, len(points), self.batch_size):
            batch = points[start:start + self.batch_size]
            result.extend(await self._batch(a, b, c, batch))
        return result

    async def _batch(self, a, b, c, points):
        coords = [gcj02_to_wgs84(p) for p in [a, b, c, *points]]
        indices = list(range(3, len(coords)))
        url = f"{self.url}/table/v1/driving/" + ';'.join(p.csv() for p in coords)

        async def table(sources, destinations):
            response = await self.client.get(url, params={
                'sources': ';'.join(map(str, sources)), 'destinations': ';'.join(map(str, destinations)),
                'annotations': 'duration,distance', 'radiuses': ';'.join(['80'] * len(coords)),
            }, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or data.get('code') != 'Ok':
                raise ValueError('unsupported_or_unavailable')
            for key, order in (('sources', sources), ('destinations', destinations)):
                waypoints = data.get(key)
                if not isinstance(waypoints, list) or len(waypoints) != len(order):
                    raise ValueError('invalid_waypoints')
                for waypoint, index in zip(waypoints, order):
                    location = waypoint.get('location')
                    if not isinstance(location, list) or len(location) != 2:
                        raise ValueError('invalid_waypoints')
                    snapped = Point(lon=location[0], lat=location[1])
                    if haversine_m(coords[index], snapped) > 80:
                        raise ValueError('remote_snap')
            for key in ('durations', 'distances'):
                matrix = data.get(key)
                if not isinstance(matrix, list) or len(matrix) != len(sources) or any(
                        not isinstance(row, list) or len(row) != len(destinations) for row in matrix):
                    raise ValueError('invalid_matrix')
            if data.get('fallback_speed_cells'):
                raise ValueError('fallback_cells')
            return data

        try:
            # Bound both requests as a unit and cancel siblings on timeout.
            first, second = await asyncio.wait_for(asyncio.gather(
                table([0], [1, *indices]), table(indices, [1, 2]), return_exceptions=True), self.timeout + .2)
            if isinstance(first, Exception) or isinstance(second, Exception):
                raise ValueError('matrix_request_failed')
            base = nonnegative(first['durations'][0][0])
            result = []
            for i in range(len(points)):
                shared = nonnegative(first['durations'][0][i + 1])
                to_b, onward = map(nonnegative, second['durations'][i])
                distance = nonnegative(second['distances'][i][1])
                if None in (base, shared, to_b, onward, distance):
                    result.append(MatrixEstimate(missing_reason='missing_cell'))
                else:
                    result.append(MatrixEstimate(shared, max(0, shared + to_b - base), onward, distance))
            return result
        except (httpx.HTTPError, ValueError, TypeError, KeyError, asyncio.TimeoutError):
            return [MatrixEstimate(missing_reason='matrix_unavailable') for _ in points]

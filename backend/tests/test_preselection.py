"""Offline regressions for coverage, ordering and quota allocation (no map calls)."""
import asyncio

from app.candidates import Candidate
from app.exit_discovery import FocusedDiscovery, SPLIT_SAMPLE_TIERS, ordinary_near_split
from app.geometry import haversine_m
from app.models import Place, Point, Preferences, RecommendationRequest, RoadSegment, Route
from app.preselection import candidate_key, select_candidates


def point(lon):
    return Point(lon=lon, lat=23.1)


def candidate(index, origin="split"):
    result = Candidate(Place(name=f"P{index}", point=point(113.3 + index * .0001)),
                       "roadside", index, None, index)
    result.origins = [origin]
    return result


def test_surface_reservation_and_empty_pool_backfill():
    primary = [candidate(i) for i in range(60)]
    surface = [candidate(i + 100, "baseline_surface") for i in range(20)]
    selected, truncated = select_candidates(primary, surface)
    assert len(selected) == 48 and truncated
    assert sum("baseline_surface" in c.origins for c in selected) == 12
    assert len(select_candidates(primary, [])[0]) == 48
    assert len(select_candidates([], surface, limit=15)[0]) == 15


def test_shared_point_merges_modes_sources_and_only_spends_one_slot():
    taxi, transit = candidate(1), candidate(1, "baseline_surface")
    taxi.modes, transit.modes = ("taxi",), ("transit",)
    selected, truncated = select_candidates([taxi], [transit], limit=2)
    assert len(selected) == 1 and not truncated
    assert set(selected[0].modes) == {"taxi", "transit"}
    assert set(selected[0].origins) == {"split", "baseline_surface"}


def test_different_exit_constraints_and_nearby_points_are_not_merged():
    first, other_exit, nearby = candidate(1), candidate(1), candidate(2)
    first.exit_place = Place(name="exit1", point=point(113.2))
    other_exit.exit_place = Place(name="exit2", point=point(113.21))
    selected, _ = select_candidates([first, other_exit, nearby], [])
    assert len(selected) == 3
    assert len({candidate_key(c) for c in selected}) == 3


def test_quota_diagnostics_identify_displaced_and_merged_candidates(monkeypatch):
    events = []
    monkeypatch.setattr("app.preselection.event", lambda name, **fields: events.append((name, fields)))
    primary = [candidate(i) for i in range(4)]
    select_candidates(primary, [candidate(100), candidate(0, "baseline_surface")], limit=3, surface_reserve=1)
    assert any(fields.get("decision") == "merged" for _, fields in events)
    assert any(fields.get("decision") == "quota_limit" for _, fields in events)
    summary = next(fields for name, fields in events if name == "preselection_summary")
    assert summary["primary_only_displaced"]


def ordinary_route():
    a, b = point(113.3), point(113.35)
    return Route(duration_s=600, distance_m=5100, polyline=[a, b],
                 segments=[RoadSegment(name="人民路", polyline=[a, b])])


def test_distance_tiers_are_independent_and_do_not_fallback_to_near_point():
    route = ordinary_route()
    found = [ordinary_near_split(route, 2000, False, offsets) for _, offsets in SPLIT_SAMPLE_TIERS]
    assert all(found)
    assert len({p.csv() for p in found}) == 3
    assert haversine_m(found[0], found[2]) > 500
    assert ordinary_near_split(route, 0, True, SPLIT_SAMPLE_TIERS[-1][1]) is None
    restricted = route.model_copy(update={"segments": [RoadSegment(name="某某高架", polyline=route.polyline)]})
    assert ordinary_near_split(restricted, 2000, False, (15, 30, 60)) is None


def request(route):
    return RecommendationRequest(origin=Place(name="A", point=route.polyline[0]),
        driver_destination=Place(name="B", point=route.polyline[-1]),
        passenger_destination=Place(name="C", point=point(113.34)), preferences=Preferences(modes=["taxi"]))


def test_completion_order_does_not_change_shortlist():
    class Discovery(FocusedDiscovery):
        async def discover(self):
            async def finish(index):
                await asyncio.sleep(.002 if index == self.slow else 0)
                c = candidate(index)
                self.annotate(c, self.routes[0], f"exit:{index}", "split")
                self.groups.append([c])
            await asyncio.gather(*(finish(i) for i in range(4)))

    async def run(slow):
        route = ordinary_route()
        discovery = Discovery()
        discovery.slow = slow
        return await discovery.run(None, request(route), [route])
    left, right = asyncio.run(run(0)), asyncio.run(run(3))
    assert [candidate_key(c) for c in left.candidates] == [candidate_key(c) for c in right.candidates]


def test_highway_route_retains_surface_supplement_without_map_calls():
    class Discovery(FocusedDiscovery):
        async def discover(self):
            pass
    a, e, b = point(113.3), point(113.33), point(113.35)
    route = Route(duration_s=600, distance_m=5100, polyline=[a, e, b], segments=[
        RoadSegment(name="G4高速", polyline=[a, e]), RoadSegment(name="人民路", polyline=[e, b])])
    discovery = asyncio.run(Discovery().run(None, request(route), [route]))
    assert discovery.candidates
    assert all("baseline_surface" in c.origins for c in discovery.candidates)
    assert all(c.place.point.lon > e.lon for c in discovery.candidates)

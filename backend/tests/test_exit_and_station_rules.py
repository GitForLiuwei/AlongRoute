import asyncio

import httpx

from app.amap import AmapClient
from app.candidates import build_candidates, controlled_segment, search_centers
from app.highway import ExitAnchor, discovered_exit, exit_was_used, highway_centers, original_exits
from app.models import ModeOption, Place, Point, Preferences, Recommendation, RecommendationRequest, RoadSegment, Route
from app.optimizer import is_viable, preferred_option, rank_recommendations

A = Point(lon=113.30, lat=23.1)
E = Point(lon=113.35, lat=23.1)
P = Point(lon=113.351, lat=23.101)
B = Point(lon=113.40, lat=23.1)


def option(mode="transit", access=200, duration=1200, fare=2):
    return ModeOption(mode=mode, duration_s=duration, total_duration_s=duration, fare_yuan=fare,
        walking_m=500 if mode == "transit" else None, transfers=0, details="线路1",
        boarding_walk_m=access if mode == "transit" else None,
        boarding_stop=Place(name="实际站台", point=E) if mode == "transit" else None)


def rec(name, selected):
    return Recommendation(id=name, point=Place(name=name, point=P), driver_detour_s=90,
        driver_route=Route(duration_s=600, distance_m=1000), options=[selected], suggested_mode=selected.mode,
        score=0, reason="test")


def test_taxi_is_not_subject_to_station_or_walking_limits():
    taxi = option("taxi")
    assert taxi.boarding_stop is None
    assert is_viable(taxi, Preferences(modes=["taxi"], max_walking_m=0, max_transfers=0))
    assert rank_recommendations([rec("taxi", taxi)], Preferences(modes=["taxi"]))


def test_mixed_modes_keep_taxi_when_transit_access_fails():
    candidate = rec("both", option("taxi"))
    candidate.options.append(option(access=301))
    ranked = rank_recommendations([candidate], Preferences(modes=["taxi", "transit"]))
    assert [item.mode for item in ranked[0].options] == ["taxi"]


def test_nearer_actual_stop_wins_when_time_and_fare_are_comparable():
    far, near = option(access=280), option(access=45, duration=1260)
    assert preferred_option([far, near], "time") is near
    ranked = rank_recommendations([rec("far", far), rec("near", near)], Preferences(priority="time"))
    assert ranked[0].id == "near"


def test_nearer_stop_does_not_erase_large_time_or_fare_difference():
    far, slow = option(access=280), option(access=40, duration=1800)
    assert preferred_option([far, slow], "time") is far
    expensive = option(access=40, fare=20)
    assert preferred_option([far, expensive], "cost") is far


def test_taxi_order_is_unaffected_by_station_access_preference():
    fast, slow = rec("fast", option("taxi", duration=1200)), rec("slow", option("taxi", duration=1260))
    assert rank_recommendations([slow, fast], Preferences(priority="time", modes=["taxi"]))[0].id == "fast"


def test_highway_mainline_generates_no_dropoff_search_centers_or_points():
    # The provider may use an interchange name while the instruction names the highway.
    segment = RoadSegment(name="某某立交", instruction="沿立交通行途经G4高速", polyline=[A, B])
    route = Route(duration_s=600, distance_m=10000, polyline=[A, B], segments=[segment])
    assert controlled_segment(segment)
    assert highway_centers(route)
    assert search_centers(route) == []
    request = RecommendationRequest(origin=Place(name="A", point=A), driver_destination=Place(name="B", point=B),
        passenger_destination=Place(name="C", point=P))
    assert build_candidates(route, [], [], request) == []


def test_highway_to_highway_interchange_is_not_an_original_exit():
    route = Route(duration_s=600, distance_m=10000, polyline=[A, E, B], segments=[
        RoadSegment(name="G4高速", polyline=[A, E]),
        RoadSegment(name="G15高速出口", instruction="经G15高速", polyline=[E, B])])
    assert original_exits(route) == []
    route.segments.append(RoadSegment(name="人民路", polyline=[B, P]))
    route.polyline.append(P)
    anchors = original_exits(route)
    assert len(anchors) == 1 and anchors[0].original
    assert anchors[0].place.point == B


def test_discovered_exit_must_be_near_the_highway_not_just_a_local_road():
    route = Route(duration_s=600, distance_m=10000, polyline=[A, B],
        segments=[RoadSegment(name="G4高速", polyline=[A, B])])
    assert discovered_exit(route, Place(name="高速出口", point=E))
    assert discovered_exit(route, Place(name="高速入口", point=E)) is None
    assert discovered_exit(route, Place(name="高速出口", point=Point(lon=E.lon, lat=E.lat + .05))) is None


def test_exit_approach_and_dropoff_must_be_reached_in_order():
    approach = Point(lon=113.34, lat=23.1)
    anchor = ExitAnchor(Place(name="某出口", point=E), approach, 5000)
    route = Route(duration_s=600, distance_m=10000, polyline=[A, approach, E, P, B])
    assert exit_was_used(route, anchor, P)
    route.polyline = [A, P, E, approach, B]
    assert not exit_was_used(route, anchor, P)


def test_waypoint_request_keeps_approach_exit_dropoff_and_full_driver_cost():
    approach = Point(lon=113.34, lat=23.1)
    def respond(request):
        assert request.url.params["waypoints"] == ";".join(p.csv() for p in [approach, E, P])
        points = [A, approach, E, P, B]
        steps = [{"road_name": "人民路", "polyline": f"{a.csv()};{b.csv()}", "cost": {"duration": "150"}}
                 for a, b in zip(points, points[1:])]
        return httpx.Response(200, json={"status": "1", "route": {"paths": [{"distance": "10000", "cost": {"duration": "600"}, "steps": steps}]}})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await AmapClient("test", client).driving(A, B, via=[approach, E, P])
    route = asyncio.run(run())
    assert route.duration_s == 600  # Includes P -> B; not just leaving the highway.
    assert route.dropoff_duration_s == 450


def test_taxi_only_highway_plan_uses_exit_and_counts_onward_driver_time(monkeypatch):
    from app.amap import calculate_live
    from app.candidates import Candidate
    request = RecommendationRequest(origin=Place(name="A", point=A), driver_destination=Place(name="B", point=B),
        passenger_destination=Place(name="C", point=Point(lon=113.45, lat=23.2)),
        preferences=Preferences(modes=["taxi"], max_detour_minutes=10, max_walking_m=0))
    seen = []
    async def driving(self, origin, destination, via=None):
        if origin == A and destination == B:
            if via:
                assert isinstance(via, list) and len(via) == 3
                seen.append(via)
                return Route(duration_s=800, distance_m=12000, dropoff_duration_s=500,
                    polyline=[A, *via, B], segments=[RoadSegment(name="G4高速", polyline=[A, E]),
                        RoadSegment(name="人民路", polyline=[E, P, B])])
            return Route(duration_s=600, distance_m=10000, polyline=[A, E, B], segments=[
                RoadSegment(name="G4高速", polyline=[A, E]), RoadSegment(name="人民路", polyline=[E, B])])
        return Route(duration_s=300, distance_m=3000, polyline=[origin, destination], fare_yuan=20)
    async def nearby(*args):
        return []
    async def transit(*args):
        raise AssertionError("Taxi-only planning must never enforce transit access")
    monkeypatch.setattr(AmapClient, "driving", driving)
    monkeypatch.setattr(AmapClient, "nearby", nearby)
    monkeypatch.setattr(AmapClient, "transit", transit)
    monkeypatch.setattr("app.amap.build_candidates", lambda *args: [Candidate(Place(name="P", point=P), "parking", 5000, None, 0)])
    result = asyncio.run(calculate_live(request))
    assert seen
    assert all(comparison.label != "从A直接前往C" for comparison in result.comparisons)
    assert len(result.recommendations) == 1
    recommendation = result.recommendations[0]
    assert recommendation.highway_exit is not None and recommendation.original_exit
    assert recommendation.driver_detour_s == 380  # Full 800 - baseline 600 + 180 stop.
    assert recommendation.options[0].boarding_walk_m is None
    assert recommendation.options[0].mode == "taxi"


def test_cancelled_discovery_request_does_not_poison_later_refinement():
    async def run():
        entered = asyncio.Event()
        calls = 0
        async def respond(request):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await asyncio.Event().wait()
            return httpx.Response(200, json={"status": "1", "pois": []})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            api = AmapClient("test", client)
            initial = asyncio.create_task(api.suggest("同一站点"))
            await entered.wait()
            initial.cancel()
            await asyncio.gather(initial, return_exceptions=True)
            assert await asyncio.wait_for(api.suggest("同一站点"), timeout=2) == []
            assert calls == 2
    asyncio.run(run())

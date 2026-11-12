import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from app.amap import AmapClient, MapServiceError, calculate_live
from app.candidates import Candidate, Station, build_candidates, restricted_road, terminal_is_usable, dropoff_road_name
from app.geometry import haversine_m
from app.models import (ModeOption, Place, Point, Preferences, Recommendation,
                        RecommendationRequest, RoadSegment, Route)
from app.optimizer import is_viable, rank_recommendations, option_score
from app.transit import coordinates


A = Point(lon=113.3, lat=23.1)
P = Point(lon=113.31, lat=23.1)
B = Point(lon=113.32, lat=23.1)
C = Point(lon=113.33, lat=23.1)
STOP = Point(lon=113.311, lat=23.1)


def itinerary(duration=600, walking=200, transfers=0, kind="公交线路", entrance=True, fee=2):
    segments = []
    for index in range(transfers + 1):
        departure = {"name": f"Station {index}", "location": STOP.csv()}
        if entrance:
            departure["entrance"] = {"name": "A口", "location": STOP.csv()}
        segments.append({"walking": {"distance": str(min(walking, 200)), "cost": {"duration": "100"}, "steps": []},
                         "bus": {"buslines": [{"name": "Line", "type": kind,
                                                "departure_stop": departure,
                                                "polyline": {"polyline": f"{STOP.csv()};{C.csv()}"}}]}})
    return {"cost": {"duration": str(duration), "transit_fee": str(fee)},
            "walking_distance": str(walking), "segments": segments}


def run_transit(monkeypatch, items, access=200, walking_calls=None, access_duration_s=100):
    async def fake_get(self, path, **params):
        assert params['AlternativeRoute'] == 4
        return {"route": {"transits": items}}

    async def walking(self, origin, destination):
        if walking_calls is not None:
            walking_calls.append((origin, destination))
        assert destination == STOP  # The selected itinerary's stop, not any nearby POI.
        return None if access is None else Route(duration_s=access_duration_s, distance_m=access,
                                                 polyline=[origin, destination])

    monkeypatch.setattr(AmapClient, "get", fake_get)
    monkeypatch.setattr(AmapClient, "walking", walking)

    async def query():
        async with httpx.AsyncClient() as client:
            return await AmapClient("test-key", client).transit(
                Place(name="P", point=P, citycode="020"), Place(name="C", point=C, citycode="020"),
                datetime(2026, 11, 4, 8, tzinfo=timezone.utc), Preferences())

    return asyncio.run(query())


@pytest.mark.parametrize("distance,accepted", [(299, True), (300, True), (301, True), (480, True), (481, False), (None, False)])
def test_actual_station_walking_boundary(monkeypatch, distance, accepted):
    options = run_transit(monkeypatch, [itinerary()], access=distance)
    assert bool(options) is accepted
    if options:
        assert options[0].boarding_walk_m == distance


@pytest.mark.parametrize("straight_m,walk_m,accepted", [(350, 480, True), (350, 481, False), (481, 500, False)])
def test_buffer_is_applied_to_both_straight_and_verified_walking_distance(monkeypatch, straight_m, walk_m, accepted):
    import math
    stop = Point(lon=P.lon, lat=P.lat + math.degrees(straight_m / 6371000))
    item = itinerary(walking=500)
    item["segments"][0]["bus"]["buslines"][0]["departure_stop"]["location"] = stop.csv()

    async def get(self, path, **params):
        return {"route": {"transits": [item]}}

    async def walking(self, origin, destination):
        return Route(duration_s=100, distance_m=walk_m, polyline=[origin, destination])

    monkeypatch.setattr(AmapClient, "get", get)
    monkeypatch.setattr(AmapClient, "walking", walking)

    async def query():
        async with httpx.AsyncClient() as client:
            return await AmapClient("test", client).transit(
                Place(name="P", point=P, citycode="020"), Place(name="C", point=C, citycode="020"),
                datetime.now(timezone.utc), Preferences())

    assert bool(asyncio.run(query())) is accepted


def test_constraints_apply_before_fastest_itinerary_selection(monkeypatch):
    options = run_transit(monkeypatch, [itinerary(duration=300, walking=3200),
                                       itinerary(duration=400, transfers=5),
                                       itinerary(duration=700, walking=500)])
    assert [option.duration_s for option in options] == [700]


def test_unknown_or_missing_metro_entrance_is_rejected(monkeypatch):
    assert run_transit(monkeypatch, [itinerary(kind="地铁线路", entrance=False)]) == []
    options = run_transit(monkeypatch, [itinerary(kind="地铁线路")])
    assert options[0].boarding_kind == "subway"
    assert options[0].boarding_stop.name.endswith("A口")


def test_nearby_unrelated_station_does_not_satisfy_access_limit(monkeypatch):
    far = itinerary()
    far["segments"][0]["bus"]["buslines"][0]["departure_stop"]["location"] = C.csv()
    assert run_transit(monkeypatch, [far]) == []


def test_bus_stop_proximity_does_not_reject_verified_transit_access(monkeypatch):
    item = itinerary()
    item["segments"][0]["bus"]["buslines"][0]["departure_stop"]["location"] = P.csv()
    # Sharing the stop coordinate no longer triggers a distance-only rejection.
    async def get(self, path, **params):
        return {"route": {"transits": [item]}}
    async def walking(self, origin, destination):
        return Route(duration_s=0, distance_m=0, polyline=[origin, destination])
    monkeypatch.setattr(AmapClient, "get", get)
    monkeypatch.setattr(AmapClient, "walking", walking)
    async def query():
        async with httpx.AsyncClient() as client:
            return await AmapClient("test-key", client).transit(
                Place(name="P", point=P, citycode="020"), Place(name="C", point=C, citycode="020"),
                datetime.now(timezone.utc), Preferences())
    options = asyncio.run(query())
    assert len(options) == 1
    assert options[0].boarding_walk_m == 0


def test_nested_provider_geometry_is_preserved():
    assert coordinates({"polyline": f"{P.csv()};{STOP.csv()}"}) == [P, STOP]


def rec(identifier, ride, onward, fee=10):
    return Recommendation(id=identifier, point=Place(name=identifier, point=P),
                          driver_detour_s=90, shared_ride_s=ride, driver_route=Route(duration_s=600, distance_m=1000),
                          options=[ModeOption(mode="taxi", duration_s=onward, fare_yuan=fee, details="test")],
                          suggested_mode="taxi", score=0, reason="test")


def test_ranking_counts_the_shared_ride_and_stop_time():
    late = rec("late", ride=1800, onward=300)
    early = rec("early", ride=300, onward=900)
    ranked = rank_recommendations([late, early], Preferences(priority="time"))
    assert ranked[0].id == "early"
    assert ranked[0].options[0].total_duration_s == 1290


def test_unknown_transit_access_cannot_survive_ranking():
    option = ModeOption(mode="transit", duration_s=300, walking_m=200, transfers=0, details="test")
    assert not is_viable(option, Preferences())


def test_cost_preference_can_choose_a_slower_transit_alternative(monkeypatch):
    options = run_transit(monkeypatch, [itinerary(duration=600, fee=15), itinerary(duration=900, fee=2)])
    recommendation = rec("choice", ride=300, onward=0)
    recommendation.options = options
    ranked = rank_recommendations([recommendation], Preferences(priority="cost", modes=["transit"]))
    chosen = min(ranked[0].options, key=lambda option: option_score(option, 'cost'))
    assert chosen.duration_s == 900
    assert chosen.fare_yuan == 2


def test_walking_verification_does_not_accept_remote_map_snaps(monkeypatch):
    async def get(self, path, **params):
        assert path == "/v5/direction/walking"
        return {"route": {"paths": [{"distance": "100", "cost": {"duration": "100"},
                                     "steps": [{"polyline": {"polyline": f"{A.csv()};{STOP.csv()}"}}]}]}}
    monkeypatch.setattr(AmapClient, "get", get)
    async def query():
        async with httpx.AsyncClient() as client:
            return await AmapClient("test-key", client).walking(P, STOP)
    assert asyncio.run(query()) is None


def test_candidate_search_expands_to_roads_and_retains_route_coverage():
    route = Route(duration_s=600, distance_m=2000, polyline=[A, B])
    request = RecommendationRequest(origin=Place(name="A", point=A), driver_destination=Place(name="B", point=B),
                                    passenger_destination=Place(name="C", point=C), preferences=Preferences(modes=["taxi"]))
    candidates = build_candidates(route, [], [], request)
    assert len(candidates) > 3
    assert all(candidate.source == "roadside" for candidate in candidates)
    assert any(candidate.progress_m < 400 for candidate in candidates)
    assert any(candidate.progress_m > 1600 for candidate in candidates)


@pytest.mark.parametrize("mode", ["taxi", "transit"])
def test_candidate_search_keeps_points_near_bus_platform(mode):
    route = Route(duration_s=600, distance_m=2000, polyline=[A, B])
    request = RecommendationRequest(origin=Place(name="A", point=A), driver_destination=Place(name="B", point=B),
                                    passenger_destination=Place(name="C", point=C), preferences=Preferences(modes=[mode]))
    near_stop = Place(name="站旁候选", point=Point(lon=P.lon + .0001, lat=P.lat))
    candidates = build_candidates(route, [near_stop], [Station(Place(name="Bus", point=P), "bus")], request)
    assert candidates
    assert any(candidate.place == near_stop for candidate in candidates)
    assert any(candidate.station_distance_m <= 300 for candidate in candidates)  # Final itineraries verify up to 480m of walking, not POI centers.


def test_highways_and_remote_road_snaps_are_rejected():
    assert restricted_road("机场高速")
    assert restricted_road("城市快速路")
    assert not restricted_road("城市快速路辅路")
    road = Route(duration_s=50, distance_m=100, polyline=[A, P],
                 segments=[RoadSegment(name="机场高速", polyline=[A, P])])
    assert not terminal_is_usable(road, P, True)
    road.segments[0].name = "普通道路"
    assert terminal_is_usable(road, P, True)
    assert not terminal_is_usable(road, C, True)


def test_transit_failure_keeps_a_valid_taxi_and_full_trip_time(monkeypatch):
    request = RecommendationRequest(origin=Place(name="A", point=A, citycode="020"),
                                    driver_destination=Place(name="B", point=B, citycode="020"),
                                    passenger_destination=Place(name="C", point=C, citycode="020"),
                                    preferences=Preferences(max_detour_minutes=5))
    async def driving(self, origin, destination, via=None):
        return Route(duration_s=600 if (origin, destination) == (A, B) else 300,
                     fare_yuan=20, distance_m=1000, polyline=[origin, via, destination] if via else [origin, destination],
                     dropoff_duration_s=300 if via else None,
                     segments=[RoadSegment(name="人民路", polyline=[origin, via, destination] if via else [origin, destination])])
    async def nearby(self, center, keyword, radius=None):
        return []
    async def transit(*args):
        raise MapServiceError("test provider failure")
    monkeypatch.setenv("AMAP_WEB_SERVICE_KEY", "test-key")
    monkeypatch.setattr(AmapClient, "driving", driving)
    monkeypatch.setattr(AmapClient, "nearby", nearby)
    monkeypatch.setattr(AmapClient, "transit", transit)
    monkeypatch.setattr("app.hub_discovery.build_candidates", lambda *args, **kwargs: [Candidate(Place(name="P", point=P), "roadside", 1000, None, 0)])
    result = asyncio.run(calculate_live(request))
    assert len(result.recommendations) == 1
    rec = result.recommendations[0]
    assert rec.driver_detour_s == 90
    assert rec.options[0].total_duration_s == 990
    assert rec.options[0].arrival_at is not None
    assert rec.options[0].mode == "taxi"
    assert any("不完整" in warning for warning in result.warnings)


def test_bus_named_metro_feeder_remains_bus(monkeypatch):
    item = itinerary(kind="普通公交")
    item["segments"][0]["bus"]["buslines"][0]["name"] = "地铁接驳专线"
    options = run_transit(monkeypatch, [item])
    assert options[0].boarding_kind == "bus"


def test_access_replaces_original_leg_in_total(monkeypatch):
    item = itinerary(walking=600)
    item["segments"][0]["walking"]["distance"] = "100"
    options = run_transit(monkeypatch, [item], access=250)
    assert options[0].walking_m == 750
    assert options[0].duration_s == 600


def test_replacement_can_push_whole_trip_over_walking_limit(monkeypatch):
    item = itinerary(walking=2950)
    item["segments"][0]["walking"]["distance"] = "100"
    assert run_transit(monkeypatch, [item], access=250) == []


def test_unknown_access_time_cannot_reuse_timetable(monkeypatch):
    item = itinerary()
    item["segments"][0]["walking"].pop("cost")
    walking_calls = []
    assert run_transit(monkeypatch, [item], walking_calls=walking_calls) == []
    assert walking_calls == []


def test_unknown_road_metadata_is_not_usable():
    assert not terminal_is_usable(Route(duration_s=20, distance_m=100, polyline=[A, P]), P, True)


def test_dropoff_name_comes_from_the_actual_nearby_driving_segment():
    route = Route(duration_s=600, distance_m=2000, polyline=[A, P, B], segments=[
        RoadSegment(name="人民路", polyline=[A, P]), RoadSegment(name="中山路", polyline=[P, B])])
    assert dropoff_road_name(route, A) == "人民路"
    assert dropoff_road_name(route, B) == "中山路"
    assert dropoff_road_name(route, C) is None  # A named remote road is not an address for P.


def test_dropoff_name_does_not_invent_missing_names_or_use_highway_labels():
    route = Route(duration_s=20, distance_m=100, polyline=[A, P], segments=[
        RoadSegment(name="高速公路", polyline=[A, P]), RoadSegment(polyline=[A, P])])
    assert dropoff_road_name(route, P) is None


def test_station_centroid_does_not_prove_entrances_are_too_far():
    route = Route(duration_s=600, distance_m=2000, polyline=[A, B])
    request = RecommendationRequest(origin=Place(name="A", point=A), driver_destination=Place(name="B", point=B),
        passenger_destination=Place(name="C", point=C), preferences=Preferences(modes=["transit"]))
    station = Place(name="Metro center", point=Point(lon=P.lon, lat=P.lat + .004))
    assert build_candidates(route, [], [Station(station, "subway")], request)


def test_taxi_ranking_balances_driver_and_passenger_time():
    faster, slower = rec("fast", 0, 1200), rec("slow", 0, 1500)
    faster.driver_detour_s = 300
    slower.driver_detour_s = 120
    assert rank_recommendations([slower, faster], Preferences(priority="time"))[0].id == "fast"


def test_continuous_waypoint_route_uses_provider_step_time():
    path = {"distance": "2000", "cost": {"duration": "600"}, "steps": [
        {"road_name": "人民路", "polyline": f"{A.csv()};{P.csv()}", "cost": {"duration": "200"}},
        {"road_name": "人民路", "polyline": f"{P.csv()};{B.csv()}", "cost": {"duration": "400"}}]}
    route = AmapClient.parse_driving({}, path, P)
    assert route.duration_s == 600
    assert route.dropoff_duration_s == 200
    path["steps"][1]["polyline"] = f"{STOP.csv()};{B.csv()}"
    assert AmapClient.parse_driving({}, path, P) is None


def test_large_walk_endpoint_gap_is_not_a_straight_line_connection(monkeypatch):
    near = Point(lon=P.lon + .0001, lat=P.lat)  # About 10m, formerly accepted.
    async def get(self, path, **params):
        return {"route": {"paths": [{"distance": "100", "cost": {"duration": "100"},
            "steps": [{"polyline": f"{near.csv()};{STOP.csv()}"}]}]}}
    monkeypatch.setattr(AmapClient, "get", get)
    async def query():
        async with httpx.AsyncClient() as client:
            return await AmapClient("test", client).walking(P, STOP)
    assert asyncio.run(query()) is None


def test_taxi_cost_preference_has_no_passenger_time_budget():
    fast = rec("fast", 0, 600, fee=20)
    slow = rec("slow", 0, 1800, fee=2)
    assert rank_recommendations([slow, fast], Preferences(priority="cost"))[0].id == "slow"


@pytest.mark.parametrize("kind,delta_s,accepted", [
    ("公交线路", 30, True), ("公交线路", 31, False),
    ("地铁线路", 31, True), ("地铁线路", 120, True), ("地铁线路", 121, False),
])
def test_access_tolerance_depends_on_boarding_kind(monkeypatch, kind, delta_s, accepted):
    item = itinerary(kind=kind)
    options = run_transit(monkeypatch, [item], access_duration_s=100 + delta_s)
    assert bool(options) is accepted
    if accepted:
        assert options[0].duration_s == 600 + delta_s


@pytest.mark.parametrize("walking,transfers,accepted", [(3000,4,True),(3001,4,False),(3000,5,False)])
def test_new_default_walking_and_transfer_boundaries(monkeypatch, walking, transfers, accepted):
    assert bool(run_transit(monkeypatch, [itinerary(walking=walking, transfers=transfers)])) is accepted

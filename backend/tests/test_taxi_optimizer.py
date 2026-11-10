import asyncio

import pytest

from app.amap import AmapClient, calculate_live
from app.candidates import Candidate, build_candidates
from app.models import (ModeOption, Place, Point, Preferences, Recommendation,
                        RecommendationRequest, RoadSegment, Route)
from app.optimizer import option_score, preferred_option, rank_recommendations
from app.manual import calculate_manual
from app.models import ManualDropoffRequest


def candidate(name, detour, fare, shared=600, onward=600, lon=113.31):
    return Recommendation(
        id=name, point=Place(name=name, point=Point(lon=lon, lat=23.1)),
        driver_detour_s=detour, shared_ride_s=shared,
        driver_route=Route(duration_s=600 + detour - 90, distance_m=2000),
        options=[ModeOption(mode="taxi", duration_s=onward, fare_yuan=fare, details="estimate")],
        suggested_mode="taxi", score=0, reason="test")


@pytest.mark.parametrize("priority,pax_threshold_s", [
    ("balanced", 108),  # 1.8 min (108s) / 1.0 pax weight = 108s
    ("time", 32),       # 0.8 min (48s) / 1.5 pax weight = 32s
    ("cost", 225),      # 3.0 min (180s) / 0.8 pax weight = 225s
])
@pytest.mark.parametrize("delta_s,worth_it", [(-1, False), (0, True), (1, True)])
def test_one_yuan_threshold_by_priority(priority, pax_threshold_s, delta_s, worth_it):
    saved_seconds = pax_threshold_s + delta_s
    cheap = candidate("cheap", 240, 20)
    fast = candidate("fast", 240, 21, shared=600 - saved_seconds)
    result = rank_recommendations([cheap, fast], Preferences(modes=["taxi"], priority=priority))
    assert result[0].id == ("fast" if worth_it else "cheap")


def test_passenger_time_prevents_old_two_dimension_dominance():
    cheap = candidate("cheap-late", 120, 30, shared=3000)
    fast = candidate("fast", 120, 31, shared=600)
    result = rank_recommendations([cheap, fast], Preferences(modes=["taxi"]))
    assert result[0].id == "fast"
    assert {rec.id for rec in result} == {"cheap-late", "fast"}


def test_old_one_minute_three_yuan_filter_cannot_erase_fast_trip():
    fast = candidate("fast", 90, 20)
    cheap_late = candidate("cheap-late", 150, 17, shared=1200)
    result = rank_recommendations([cheap_late, fast], Preferences(modes=["taxi"]))
    assert result[0].id == "fast"


def test_ranking_removes_only_candidates_worse_in_all_three_dimensions():
    fast = candidate("fast", 90, 20, shared=600)
    worse = candidate("worse", 120, 21, shared=660)
    assert [rec.id for rec in rank_recommendations([worse, fast], Preferences(modes=["taxi"]))] == ["fast"]


def test_shared_ride_and_stop_time_are_included_without_mutating_inputs():
    late = candidate("late", 90, 20, shared=3600)
    early = candidate("early", 120, 20, shared=1)
    result = rank_recommendations([late, early], Preferences(modes=["taxi"]))
    assert result[0].id == "early"
    assert result[0].options[0].total_duration_s == 691
    assert late.options[0].total_duration_s is None
    assert early.recommendation_label == ""


def test_decimal_fares_respect_exact_balanced_boundary():
    cheap = candidate("cheap", 90, 6.94)
    fast = candidate("fast", 90, 7.94, shared=492)
    assert rank_recommendations([cheap, fast], Preferences(modes=["taxi"]))[0].id == "fast"


@pytest.mark.parametrize("priority,threshold_s", [("balanced", 72), ("time", 32), ("cost", 150)])
@pytest.mark.parametrize("delta_s,worth_it", [(-1, False), (0, True), (1, True)])
def test_driver_time_tradeoff_by_priority(priority, threshold_s, delta_s, worth_it):
    cheap = candidate("cheap", 300, 20)
    fast = candidate("fast", 300 - threshold_s - delta_s, 21)
    result = rank_recommendations([cheap, fast], Preferences(modes=["taxi"], priority=priority))
    assert result[0].id == ("fast" if worth_it else "cheap")


def test_nearby_similar_taxi_points_are_collapsed_but_distinct_trips_remain():
    best = candidate("best", 90, 20, shared=600)
    nearby = candidate("nearby", 120, 20, shared=590, lon=113.311)
    far = candidate("far", 150, 20, shared=580, lon=113.32)
    cheaper = candidate("cheaper", 90, 19, shared=620, lon=113.311)
    faster = candidate("faster", 180, 20, shared=300, lon=113.311)
    result = rank_recommendations([best, nearby, far, cheaper, faster], Preferences(modes=["taxi"]))
    assert {rec.id for rec in result} == {"best", "far", "cheaper", "faster"}


@pytest.mark.parametrize("b_fare,first_lon", [(20, 113.32), (None, 113.31)])
def test_live_demotes_trips_worse_than_staying_to_b(monkeypatch, b_fare, first_lon):
    a, b, c = Point(lon=113.3, lat=23.1), Point(lon=113.4, lat=23.1), Point(lon=113.5, lat=23.1)
    points = [Point(lon=113.31, lat=23.1), Point(lon=113.32, lat=23.1)]
    req = RecommendationRequest(origin=Place(name="A", point=a), driver_destination=Place(name="B", point=b),
        passenger_destination=Place(name="C", point=c), preferences=Preferences(modes=["taxi"]))

    async def discover(self, api, request, routes):
        self.candidates = [Candidate(Place(name="P", point=p), "roadside", 0, None, 0) for p in points]
        return self

    async def driving(self, origin, destination, via=None):
        if via is not None:
            index = points.index(via)
            return Route(duration_s=610 if index == 0 else 900, distance_m=10000,
                dropoff_duration_s=100 if index == 0 else 400, polyline=[a, via, b],
                segments=[RoadSegment(name="普通道路", polyline=[a, via, b])])
        duration, fare = ((1000, 20) if origin == points[0] else (100, 30)) if origin in points else (100, b_fare)
        if destination == b:
            duration = 600
        return Route(duration_s=duration, distance_m=10000, fare_yuan=fare, polyline=[origin, destination],
            segments=[RoadSegment(name="普通道路", polyline=[origin, destination])])

    monkeypatch.setattr("app.hub_discovery.HubDiscovery.run", discover)
    monkeypatch.setattr(AmapClient, "driving", driving)
    result = asyncio.run(calculate_live(req))
    assert result.recommendations[0].point.point.lon == first_lon
    assert result.recommendations[0].recommendation_label == "综合推荐"
    assert result.recommendations[0].reason.startswith("综合推荐：")


@pytest.mark.parametrize("priority,expected", [("balanced", 3275 / 60), ("time", 2520 / 60), ("cost", 4492 / 60)])
def test_manual_score_uses_selected_priority(monkeypatch, priority, expected):
    a, b, c, p = [Point(lon=lon, lat=23.1) for lon in (113.3, 113.4, 113.5, 113.32)]
    async def driving(self, origin, destination, via=None):
        return Route(duration_s=660 if via else (400 if origin == p else 600), distance_m=10000,
            dropoff_duration_s=100 if via else None, fare_yuan=20, polyline=[origin, destination])
    monkeypatch.setattr(AmapClient, "driving", driving)
    req = ManualDropoffRequest(origin=Place(name="A", point=a), driver_destination=Place(name="B", point=b),
        passenger_destination=Place(name="C", point=c), dropoff=Place(name="P", point=p),
        preferences=Preferences(priority=priority))
    result = asyncio.run(calculate_manual(req))
    assert result.combined_score == pytest.approx(expected)


def test_top_six_are_ordered_by_combined_score():
    recs = [candidate(str(index), 90, 20 + index, shared=1000 - 100 * index) for index in range(8)]
    result = rank_recommendations(list(reversed(recs)), Preferences(modes=["taxi"]))
    assert [rec.id for rec in result] == ["0", "1", "2", "3", "4", "5"]
    assert [rec.score for rec in result] == sorted(rec.score for rec in result)


def test_time_savings_never_relax_hard_detour_or_fare_limits():
    result = rank_recommendations([
        candidate("within", 300, 200), candidate("detour-over", 301, 1, shared=0),
        candidate("fare-over", 90, 200.01, shared=0)],
        Preferences(modes=["taxi"], max_detour_minutes=5))
    assert [rec.id for rec in result] == ["within"]
    assert rank_recommendations([candidate("stop", 90, 20)],
                                Preferences(modes=["taxi"], max_detour_minutes=0)) == []


@pytest.mark.parametrize("unknown_fare", [None, -1, float("nan"), float("inf")])
def test_unknown_fare_cannot_pass_budget(unknown_fare):
    result = rank_recommendations([
        candidate("unknown", 90, unknown_fare), candidate("known", 180, 20)],
        Preferences(modes=["taxi"]))
    assert [rec.id for rec in result] == ["known"]


def transit_candidate(name, duration, fee):
    rec = candidate(name, 90, fee)
    rec.options = [ModeOption(mode="transit", duration_s=duration, fare_yuan=fee,
        walking_m=200, transfers=0, boarding_walk_m=100,
        boarding_stop=Place(name="stop", point=rec.point.point), details="transit")]
    return rec


@pytest.mark.parametrize("priority,first_bonus,transfer_bonus", [
    ("balanced", 5, 2.5), ("time", 3, 1.5), ("cost", 0, 0),
])
def test_subway_reliability_bonus(priority, first_bonus, transfer_bonus):
    from app.models import TransitStep
    bus = transit_candidate("bus", 1200, 3).options[0]
    bus.boarding_kind = "bus"
    subway = bus.model_copy(update={"boarding_kind": "subway"}, deep=True)
    transfer = bus.model_copy(update={"transit_steps": [TransitStep(kind="subway", title="地铁")]}, deep=True)
    assert option_score(bus, priority) - option_score(subway, priority) == pytest.approx(first_bonus)
    assert option_score(bus, priority) - option_score(transfer, priority) == pytest.approx(transfer_bonus)


@pytest.mark.parametrize("priority", ["balanced", "time"])
def test_shorter_bus_walk_cannot_replace_winning_subway(priority):
    subway = transit_candidate("subway", 1200, 3)
    subway.options[0].boarding_kind = "subway"
    subway.options[0].boarding_walk_m = 200
    bus = transit_candidate("bus", 1260, 3)
    bus.options[0].boarding_kind = "bus"
    bus.options[0].boarding_walk_m = 10
    assert preferred_option([bus.options[0], subway.options[0]], priority).boarding_kind == "subway"
    assert rank_recommendations([bus, subway], Preferences(modes=["transit"], priority=priority))[0].id == "subway"


@pytest.mark.parametrize("walk,transit_fare,priority,expected", [
    (0, 3, "balanced", "transit"), (200, 3, "cost", "transit"),
    (201, 3, "balanced", "taxi"), (100, None, "balanced", "taxi"),
    (100, 3, "time", "taxi"),
])
def test_same_point_subway_switch_and_dual_summary(walk, transit_fare, priority, expected):
    rec = candidate("both", 90, 25)
    transit = transit_candidate("subway", 900, transit_fare).options[0]
    transit.boarding_kind, transit.boarding_walk_m = "subway", walk
    rec.options.append(transit)
    original = rec.model_dump()
    result = rank_recommendations([rec], Preferences(priority=priority))[0]
    assert result.suggested_mode == expected
    assert {o.mode for o in result.options} == {"taxi", "transit"}
    assert "同点位亦可" in result.reason
    assert ("同点位亦可打车" if expected == "transit" else "同点位亦可乘地铁") in result.reason
    assert rec.model_dump() == original
    if expected == "transit":
        assert "后续建议公共交通" in result.reason
        assert result.recommendation_label == ""


def test_slow_bus_switches_to_taxi_and_keeps_other_transit():
    both = transit_candidate("both", 2400, 2)
    both.options[0].boarding_kind = "bus"
    both.options.append(ModeOption(mode="taxi", duration_s=600, fare_yuan=12, details="taxi"))
    cheap = candidate("taxi-first", 90, 5, shared=0, lon=113.4)
    other = transit_candidate("other-subway", 900, 4)
    other.point = Place(name="other", point=Point(lon=113.5, lat=23.1))
    other.options[0].boarding_stop = other.point
    other.options[0].boarding_kind = "subway"
    result = rank_recommendations([both, cheap, other], Preferences())
    switched = next(r for r in result if r.id == "both")
    assert switched.suggested_mode == "taxi"
    assert "后续建议打车" in switched.reason
    assert "同点位亦可乘公交" in switched.reason
    assert any(r.suggested_mode == "transit" for r in result)


def test_dual_summary_uses_preferred_transit_and_respects_hard_limits():
    rec = candidate("both", 90, 12)
    slow = transit_candidate("slow", 2400, 5).options[0]
    fast = transit_candidate("fast", 900, 2).options[0]
    slow.boarding_kind, fast.boarding_kind = "bus", "subway"
    rec.options.extend([slow, fast])
    result = rank_recommendations([rec], Preferences())[0]
    assert "同点位亦可乘地铁" in result.reason
    fast.boarding_walk_m = slow.boarding_walk_m = 301
    result = rank_recommendations([rec], Preferences())[0]
    assert "同点位亦可" not in result.reason
    assert [o.mode for o in result.options] == ["taxi"]


def test_transit_keeps_fifteen_minute_cost_budget():
    fast = transit_candidate("fast", 600, 20)
    slow = transit_candidate("slow", 1800, 2)
    result = rank_recommendations([slow, fast], Preferences(modes=["transit"], priority="cost"))
    assert result[0].id == "fast"


@pytest.mark.parametrize("priority", ["time", "cost"])
def test_transit_preference_retains_driver_detour_weight(priority):
    low_detour = transit_candidate("low-detour", 600, 2)
    fast = transit_candidate("fast", 550, 2)
    fast.driver_detour_s = 570
    result = rank_recommendations([fast, low_detour], Preferences(modes=["transit"], priority=priority))
    assert result[0].id == "low-detour"


def test_mixed_modes_keep_taxi_independent_of_fast_transit():
    taxi = candidate("taxi", 90, 20, shared=3600, lon=113.32)
    transit = transit_candidate("transit", 600, 2)
    result = rank_recommendations([taxi, transit], Preferences(priority="cost"))
    assert {r.suggested_mode for r in result} == {"taxi", "transit"}


def test_taxi_discovery_includes_shared_ride_time():
    a, c = Point(lon=113.30, lat=23.1), Point(lon=113.40, lat=23.1)
    b = Point(lon=113.399, lat=23.1)
    request = RecommendationRequest(origin=Place(name="A", point=a),
        driver_destination=Place(name="C", point=c), passenger_destination=Place(name="B", point=b),
        preferences=Preferences(modes=["taxi"]))
    def discover(duration):
        return build_candidates(Route(duration_s=duration, distance_m=10000, polyline=[a, c]), [], [], request)
    fast, slow = discover(600), discover(6000)
    estimates = {r.place.point.csv(): r.estimate_s for r in fast}
    common = [r for r in slow if r.place.point.csv() in estimates]
    assert common
    assert all(r.estimate_s >= estimates[r.place.point.csv()] for r in common)
    assert any(r.estimate_s > estimates[r.place.point.csv()] for r in common)


def test_live_search_does_not_stop_after_six_feasible_taxi_points(monkeypatch):
    a, c, b = Point(lon=113.30, lat=23.1), Point(lon=113.40, lat=23.1), Point(lon=113.45, lat=23.1)
    points = [Point(lon=113.31 + i * .005, lat=23.1) for i in range(9)]
    request = RecommendationRequest(origin=Place(name="A", point=a),
        driver_destination=Place(name="C", point=c), passenger_destination=Place(name="B", point=b),
        preferences=Preferences(modes=["taxi"], priority="cost", max_detour_minutes=10))
    evaluated = []

    async def driving(self, origin, destination, via=None):
        if via is not None:
            evaluated.append(via)
            index = points.index(via)
            return Route(duration_s=600 + index * 30, distance_m=10000,
                dropoff_duration_s=300, polyline=[a, via, c],
                segments=[RoadSegment(name="普通道路", polyline=[a, via, c])])
        if (origin, destination) == (a, c):
            return Route(duration_s=600, distance_m=10000, polyline=[a, c],
                segments=[RoadSegment(name="普通道路", polyline=[a, c])])
        return Route(duration_s=600, distance_m=3000, fare_yuan=80 - points.index(origin) * 5 if origin in points else 90,
                     polyline=[origin, destination])

    async def nearby(*args, **kwargs):
        return []

    monkeypatch.setattr(AmapClient, "driving", driving)
    monkeypatch.setattr(AmapClient, "nearby", nearby)
    monkeypatch.setattr("app.hub_discovery.build_candidates", lambda *args, **kwargs: [
        Candidate(Place(name=f"P{i}", point=p), "roadside", i * 500, None, 0) for i, p in enumerate(points)])
    result = asyncio.run(calculate_live(request))
    assert len(evaluated) == 9
    assert result.recommendations[0].point.point == points[-1]
    assert result.recommendations[0].options[0].fare_yuan == 40
    assert result.recommendations[0].point.name == "普通道路 · 下车候选"
    assert result.recommendations[0].dropoff_road == "普通道路"
    assert "元" in result.recommendations[0].comparison
    assert not any("15分钟" in warning for warning in result.warnings)


@pytest.mark.parametrize("modes", [["transit"], ["taxi", "transit"]])
@pytest.mark.parametrize("count", [2, 6, 8])
def test_recommendation_count_for_transit_and_mixed_modes(modes, count):
    recs = []
    for index in range(count):
        rec = candidate(str(index), 90, 20 + index, shared=1000 - 100 * index,
                        lon=113.31 + index * .001)
        transit = transit_candidate(str(index), 1500 - 100 * index, 2 + index).options[0]
        transit.boarding_stop = rec.point.model_copy(deep=True)
        rec.options.append(transit)
        recs.append(rec)
    result = rank_recommendations(recs, Preferences(modes=modes))
    assert len(result) == min(count, 6)
    assert len({rec.point.point.csv() for rec in result}) == len(result)
    if len(modes) == 2:
        assert all({option.mode for option in rec.options} == {"taxi", "transit"} for rec in result)

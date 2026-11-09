import asyncio

import pytest

from app.amap import AmapClient, calculate_live
from app.candidates import Candidate, build_candidates
from app.models import (ModeOption, Place, Point, Preferences, Recommendation,
                        RecommendationRequest, RoadSegment, Route)
from app.optimizer import rank_recommendations


def candidate(name, detour, fare, shared=600, onward=600, lon=113.31):
    return Recommendation(
        id=name, point=Place(name=name, point=Point(lon=lon, lat=23.1)),
        driver_detour_s=detour, shared_ride_s=shared,
        driver_route=Route(duration_s=600 + detour - 90, distance_m=2000),
        options=[ModeOption(mode="taxi", duration_s=onward, fare_yuan=fare, details="estimate")],
        suggested_mode="taxi", score=0, reason="test")


@pytest.mark.parametrize("priority", ["balanced", "time", "cost"])
@pytest.mark.parametrize("saved_seconds,worth_it", [(179, False), (180, True), (181, True)])
@pytest.mark.parametrize("driver_saved", [0, 60, 120])
def test_one_yuan_for_three_combined_minutes(priority, saved_seconds, worth_it, driver_saved):
    cheap = candidate("cheap", 240, 20)
    fast = candidate("fast", 240 - driver_saved, 21,
                     shared=600 - (saved_seconds - driver_saved))
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


def test_decimal_fares_respect_exact_three_minute_boundary():
    cheap = candidate("cheap", 90, 6.94)
    fast = candidate("fast", 90, 7.94, shared=420)
    assert rank_recommendations([cheap, fast], Preferences(modes=["taxi"]))[0].id == "fast"


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


def test_transit_keeps_fifteen_minute_cost_budget():
    fast = transit_candidate("fast", 600, 20)
    slow = transit_candidate("slow", 1800, 2)
    result = rank_recommendations([slow, fast], Preferences(modes=["transit"], priority="cost"))
    assert result[0].id == "fast"


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

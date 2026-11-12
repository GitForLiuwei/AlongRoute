"""Offline regressions for the attached algorithm upgrade's behavioral changes."""
import asyncio
import math

import httpx
import pytest

from app.amap import AmapClient, MapServiceError, RequestGate, _compact_line
from app.candidates import Candidate
from app.coarse_routing import estimate_candidates, geometry_estimate
from app.exit_discovery import common_prefix_end
from app.geometry import haversine_m
from app.highway import _is_closed_motorway, original_exits
from app.models import (ModeOption, Place, Point, Preferences, Recommendation,
                        RecommendationRequest, RoadSegment, Route, TransitStep)
from app.optimizer import is_viable, option_score, rank_recommendations, rank_taxi_recommendations
from app.preselection import select_candidates


def point(x, y=0):
    # Metres near the equator, so the geometry is easy to inspect.
    return Point(lon=math.degrees(x / 6371000), lat=math.degrees(y / 6371000))


def route(coords, name="人民路", duration=1000):
    points = [point(*coord) for coord in coords]
    return Route(duration_s=duration, distance_m=10000, polyline=points,
                 segments=[RoadSegment(name=name, polyline=points)])


def candidate(x, y=0, score=0):
    return Candidate(Place(name=f"P{x}", point=point(x, y)), "roadside", x, None, score)


def transit(walk=300, line="1号线"):
    return ModeOption(mode="transit", duration_s=1000, walking_m=1000, fare_yuan=3,
                      boarding_walk_m=walk, transfers=0, details="metro", boarding_kind="subway",
                      boarding_stop=Place(name="地铁入口", point=point(1000)),
                      transit_steps=[TransitStep(kind="subway", title="乘坐地铁", line_name=line)])


def test_gate_releases_lock_before_sleep_and_reserves_distinct_slots(monkeypatch):
    async def run():
        gate = RequestGate(interval=1)
        delays = []

        async def sleep(delay):
            assert not gate.lock.locked()
            delays.append(delay)

        monkeypatch.setattr("app.amap.asyncio.sleep", sleep)
        await asyncio.gather(*(gate.wait() for _ in range(4)))
        assert len(delays) == 3
        assert delays == pytest.approx([1, 2, 3], abs=0.05)

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["timeout", "http", "json"])
@pytest.mark.parametrize("permanent", [False, True])
def test_transport_and_decode_failures_retry_with_bounded_backoff(monkeypatch, failure, permanent):
    attempts, delays = [], []

    async def no_gate(path):
        pass

    async def sleep(delay):
        delays.append(delay)

    def respond(request):
        attempts.append(request)
        if not permanent and len(attempts) == 3:
            return httpx.Response(200, json={"status": "1"})
        if failure == "timeout":
            raise httpx.ReadTimeout("transient", request=request)
        if failure == "http":
            return httpx.Response(503)
        return httpx.Response(200, text="invalid JSON")

    monkeypatch.setattr("app.amap.wait_for_request_slot", no_gate)
    monkeypatch.setattr("app.amap.asyncio.sleep", sleep)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await AmapClient("private-test-key", client).get("/retry")

    if permanent:
        with pytest.raises(MapServiceError, match="暂不可用") as exc:
            asyncio.run(run())
        assert "private-test-key" not in str(exc.value)
    else:
        assert asyncio.run(run()) == {"status": "1"}
    assert len(attempts) == 3
    assert delays == [0.2, 0.4]


def test_compact_polyline_keeps_sharp_bend_endpoints_and_budget():
    points = [point(i * 10, 100 if i == 51 else 0) for i in range(101)]
    result = _compact_line(points)
    assert len(result) <= 28
    assert points[51].model_dump(mode="json") in result
    assert result[0] == points[0].model_dump(mode="json")
    assert result[-1] == points[-1].model_dump(mode="json")
    assert [p["lon"] for p in result] == sorted(p["lon"] for p in result)
    assert len(_compact_line(points, 2)) == 2
    assert _compact_line([]) == []
    assert _compact_line(points[:2]) == [p.model_dump(mode="json") for p in points[:2]]


def test_prefix_matches_aliases_but_rejects_parallel_levels_and_reverse_heading():
    driver = route([(0, 0), (1000, 0)], "G4京港澳高速主路")
    assert common_prefix_end(driver, route([(0, 0), (1000, 0)], "京港澳高速")) == pytest.approx(1000)
    assert common_prefix_end(driver, route([(0, 0), (1000, 0)], "京港澳辅路")) == 0
    assert common_prefix_end(driver, route([(0, 0), (-1000, 0)], "京港澳高速")) == 0
    assert common_prefix_end(driver, route([(0, 30), (1000, 30)], "京港澳高速")) is None


def test_prefix_skips_short_braid_before_final_divergence():
    driver = route([(0, 0), (1000, 0)])
    braided = route([(0, 0), (100, 0), (150, 20), (250, 20), (300, 0), (600, 0), (800, 200)])
    assert 580 < common_prefix_end(driver, braided) < 640
    long_split = route([(0, 0), (100, 0), (150, 30), (700, 30), (750, 0), (1000, 0)])
    assert common_prefix_end(driver, long_split) < 160


def test_prefix_does_not_treat_a_later_crossing_as_reunion():
    driver = route([(0, 0), (1000, 0)])
    crossing = route([(0, 0), (100, 0), (200, 50), (300, -50), (800, -50)])
    assert common_prefix_end(driver, crossing) < 140


@pytest.mark.parametrize("known_shared", [None, 600])
def test_coarse_estimate_is_continuous_around_detour_threshold(known_shared):
    baseline = route([(0, 0), (20000, 0)])
    estimates = []
    for gap in (1799.9, 1800, 1800.1, 2999.9, 3000, 3000.1):
        c = candidate(10000, gap)
        c.approach_duration_s = known_shared
        estimates.append(geometry_estimate(c, [baseline], point(10000, 6000)))
    for left, right in ((0, 1), (1, 2), (3, 4), (4, 5)):
        for key in ("shared_s", "detour_s"):
            assert abs(estimates[left][key] - estimates[right][key]) < 1


def test_coarse_lengths_are_computed_once_per_batch(monkeypatch):
    baseline = route([(0, 0), (10000, 0), (20000, 0)])
    req = RecommendationRequest(origin=Place(name="A", point=point(0)),
        driver_destination=Place(name="B", point=point(20000)),
        passenger_destination=Place(name="C", point=point(15000, 1000)))
    calls = []

    def measure(a, b):
        calls.append((a, b))
        return haversine_m(a, b)

    monkeypatch.setattr("app.coarse_routing.haversine_m", measure)
    asyncio.run(estimate_candidates(None, req, [baseline], [candidate(i * 1000) for i in range(10)]))
    assert calls.count((baseline.polyline[0], baseline.polyline[1])) == 1
    # A new batch must see edits to mutable route geometry.
    baseline.polyline[1] = point(10000, 1000)
    asyncio.run(estimate_candidates(None, req, [baseline], [candidate(5000)]))
    assert (baseline.polyline[0], baseline.polyline[1]) in calls


def test_local_detour_does_not_depend_on_remote_route_turns():
    straight = route([(0, 0), (20000, 0)], duration=500)
    winding = route([(0, 0), (20000, 0), (20000, 20000), (0, 20000), (0, 1000)])
    c = candidate(10000, -4000)
    short = geometry_estimate(c, [straight], point(10000, -6000))
    long = geometry_estimate(c, [winding], point(10000, -6000))
    assert long["detour_s"] == pytest.approx(short["detour_s"], abs=0.1)


@pytest.mark.parametrize("priority", ["balanced", "time", "cost"])
def test_boarding_buffer_adds_real_score_penalty_without_relaxing_other_limits(priority):
    near, far = transit(), transit(450)
    assert option_score(far, priority) - option_score(near, priority) == pytest.approx(5)
    assert is_viable(transit(480), Preferences())
    assert not is_viable(transit(481), Preferences())
    assert not is_viable(far, Preferences(max_walking_m=999))
    assert not is_viable(far.model_copy(update={"transfers": 5}), Preferences())
    taxi = ModeOption(mode="taxi", duration_s=1000, fare_yuan=3, details="taxi")
    assert option_score(taxi, priority) == option_score(taxi.model_copy(update={"boarding_walk_m": 450}), priority)


def test_taxi_dedup_preserves_distinct_verified_subway_lines():
    def rec(name, x, line):
        return Recommendation(id=name, point=Place(name=name, point=point(x)),
            driver_detour_s=90, driver_route=route([(0, 0), (5000, 0)]),
            options=[ModeOption(mode="taxi", duration_s=600, fare_yuan=20, details="taxi"), transit(line=line)],
            dropoff_road="人民路", suggested_mode="taxi", score=0, reason="test")

    first, other_line = rec("first", 1000, "1号线"), rec("other", 2000, "2号线")
    prefs = Preferences()
    kept = rank_taxi_recommendations([first, other_line], prefs)
    assert {r.id for r in kept} == {"first", "other"}
    assert all(len(r.options) == 1 and "station_lines" not in r.model_dump() for r in kept)
    assert len(rank_taxi_recommendations([first, rec("same", 2000, "1号线")], prefs)) == 1
    assert len(rank_taxi_recommendations([first, rec("nearby", 1100, "2号线")], prefs)) == 1
    # Different later transfers do not prove distinct lines at the boarding station.
    later = rec("later", 2000, "1号线")
    later.options[1].transit_steps.append(TransitStep(kind="subway", title="换乘", line_name="3号线"))
    assert len(rank_taxi_recommendations([first, later], prefs)) == 1


@pytest.mark.parametrize("priority", ["balanced", "time", "cost"])
def test_buffer_penalty_affects_recommendation_order(priority):
    def rec(name, x, walk, duration):
        return Recommendation(id=name, point=Place(name=name, point=point(x)),
            driver_detour_s=90, driver_route=route([(0, 0), (5000, 0)]),
            options=[transit(walk).model_copy(update={"duration_s": duration})],
            suggested_mode="transit", score=0, reason="test")

    near, far = rec("near", 1000, 300, 1000), rec("far", 2000, 450, 800)
    result = rank_recommendations([far, near], Preferences(modes=["transit"], priority=priority))
    assert result[0].id == "near"


@pytest.mark.parametrize("name", ["城市高架", "快速路", "内环主路", "中环", "外环", "三环主路", "四环主路", "五环主路", "隧道", "跨江大桥"])
def test_closed_road_recognition_excludes_surface_variants(name):
    assert _is_closed_motorway(RoadSegment(name=name))
    for suffix in ("辅路", "地面", "辅道", "桥下"):
        assert not _is_closed_motorway(RoadSegment(name=name + suffix))


def test_highway_to_elevated_transition_is_not_an_exit():
    baseline = route([(0, 0), (1000, 0), (2000, 0), (3000, 0)])
    baseline.segments = [RoadSegment(name=name, polyline=baseline.polyline[i:i + 2])
                         for i, name in enumerate(("G4高速", "城市高架", "人民路"))]
    assert [a.place.point for a in original_exits(baseline)] == [point(2000)]


def test_preselection_reranks_after_duplicate_estimate_improves():
    first, second, improved = candidate(1000, score=100), candidate(2000, score=50), candidate(1000, score=10)
    selected, truncated = select_candidates([first, second, improved], [], limit=1)
    assert truncated and selected == [first] and selected[0].estimate_s == 10
    surface = candidate(3000, score=200)
    selected, _ = select_candidates([second, first], [surface], limit=2, surface_reserve=1)
    assert selected == [first, surface]

"""Regressions for hub anchoring, coarse routing, and final card consolidation."""
import asyncio

import pytest

from app.candidates import Candidate, build_candidates
from app.coarse_routing import geometry_estimate, taxi_priority
from app.geometry import haversine_m, project_to_path
from app.hub_discovery import HubDiscovery
from app.hub_discovery import _divergence_corridor_anchors
from app.models import ModeOption, Place, Point, Preferences, Recommendation, RecommendationRequest, RoadSegment, Route
from app.optimizer import rank_recommendations


def point(lon, lat=23.1):
    return Point(lon=lon, lat=lat)


def request():
    return RecommendationRequest(
        origin=Place(name='A', point=point(113)),
        driver_destination=Place(name='B', point=point(114)),
        passenger_destination=Place(name='C', point=point(113.15)),
        preferences=Preferences(modes=['taxi']))


def test_surface_quota_stays_near_closest_approach_instead_of_passing_destination():
    req = request()
    path = [point(113 + i * .01) for i in range(101)]
    route = Route(duration_s=6000, distance_m=102000, polyline=path,
                  segments=[RoadSegment(name='普通道路', polyline=[a, b]) for a, b in zip(path, path[1:])])
    selected = build_candidates(route, [], [], req, limit=12)
    length = sum(haversine_m(a, b) for a, b in zip(path, path[1:]))
    _, q_progress, _ = project_to_path(req.passenger_destination.point, path)
    assert len(selected) == 12
    assert max(c.progress_m for c in selected) <= max(length * .35, q_progress + 8000) * 1.05
    assert any(c.progress_m < q_progress for c in selected)
    assert any(c.progress_m > q_progress for c in selected)


def test_branch_estimate_uses_highway_speed_and_preserves_observed_shared_time():
    route = Route(duration_s=3600, distance_m=102000, polyline=[point(113), point(114)])
    candidate = Candidate(Place(name='分叉枢纽', point=point(113.5, 23.17)), 'station_area', 0, None, 0)
    _, _, gap = project_to_path(candidate.place.point, route.polyline)
    estimate = geometry_estimate(candidate, [route], point(113.5, 23.2))
    assert 0 < estimate['detour_s'] < (2 * gap / (32 / 3.6)) * .8
    candidate.approach_duration_s = 2500
    observed = geometry_estimate(candidate, [route], point(113.5, 23.2))
    assert observed['shared_s'] == 2500
    assert observed['detour_s'] >= 0


@pytest.mark.parametrize('priority,expected', [('balanced', 'cheap'), ('time', 'fast'), ('cost', 'cheap')])
def test_coarse_taxi_order_tracks_requested_preference(priority, expected):
    cheap = Candidate(Place(name='cheap', point=point(113.2)), 'roadside', 0, None, 0,
                      coarse=dict(detour_s=100, shared_s=1000, onward_s=100, onward_m=1000))
    fast = Candidate(Place(name='fast', point=point(113.3)), 'roadside', 0, None, 0,
                     coarse=dict(detour_s=100, shared_s=100, onward_s=100, onward_m=7000))
    assert min([cheap, fast], key=lambda c: taxi_priority(c, priority)).place.name == expected


def test_approach_falls_back_to_named_road_before_unnamed_driveway():
    seed_point = point(113.5)
    road_end = point(113.5 - .0038)
    route = Route(duration_s=1800, distance_m=51000,
                  polyline=[point(113), road_end, seed_point],
                  segments=[RoadSegment(name='普通道路', polyline=[point(113), road_end]),
                            RoadSegment(name='', polyline=[road_end, seed_point])])

    class API:
        async def driving(self, origin, destination):
            return route

    discovery = HubDiscovery()
    discovery.api, discovery.request = API(), request()
    seed = Candidate(Place(name='地铁入口', point=seed_point), 'station_area', 0, None, 0,
                     hub_kind='subway', region='hub:0')
    asyncio.run(discovery.approach(seed))
    assert discovery.approached
    candidate = discovery.approached[0]
    assert candidate.place.point == road_end
    assert 300 < haversine_m(candidate.place.point, seed_point) <= 450
    assert candidate.approach_duration_s == route.duration_s


def test_seeds_stay_with_recall_center_and_keep_outer_region_separate():
    center = point(113.5, 23.14)
    local = Place(name='中心站', point=center)
    remote = Place(name='主线地铁A口', point=point(113.5, 23.102))

    class API:
        key = 'test'

        async def nearby(self, center, label, radius):
            return [remote, local]

    discovery = HubDiscovery()
    discovery.api, discovery.request = API(), request()
    discovery.routes = [Route(duration_s=600, distance_m=100000, polyline=[point(113), point(114)])]
    asyncio.run(discovery.fetch(center, '地铁站', 'subway', 0))
    selected = list(discovery.seeds.values())
    assert [c.place for c in selected] == [local, remote]
    assert [c.region for c in selected] == ['hub:0:w', 'hub:0:outer']


def test_divergence_anchor_prioritizes_bridge_before_destination_and_split():
    driver = Route(duration_s=3600, distance_m=100000, polyline=[point(113), point(114)])
    destination = point(113.6, 23.25)
    passenger = Route(duration_s=3000, distance_m=80000,
                      polyline=[point(113), point(113.6), destination])
    anchors = _divergence_corridor_anchors(passenger, [driver], destination)
    assert anchors
    assert 2800 <= project_to_path(anchors[0], driver.polyline)[2] <= 12000
    assert haversine_m(anchors[0], destination) >= 4000


def rec(name, lon, detour=90, fare=20, shared=600):
    return Recommendation(
        id=name, point=Place(name=name, point=point(lon)), driver_detour_s=detour,
        driver_route=Route(duration_s=600 + detour - 90, distance_m=10000), shared_ride_s=shared,
        options=[ModeOption(mode='taxi', duration_s=600, fare_yuan=fare, details='taxi')],
        suggested_mode='taxi', score=0, reason='test')


def transit(duration=900, fare=3, lon=113.3):
    return ModeOption(mode='transit', duration_s=duration, fare_yuan=fare, walking_m=100,
                      transfers=0, boarding_walk_m=100, boarding_kind='subway',
                      boarding_stop=Place(name='入口', point=point(lon)), details='subway')


@pytest.mark.parametrize('modes', [['transit'], ['taxi', 'transit']])
@pytest.mark.parametrize('detour', [-1, 301])
def test_transit_cannot_bypass_driver_detour_limit(modes, detour):
    valid = rec('valid', 113.3, detour=300)
    valid.options = [transit(duration=1800)]
    invalid = rec('invalid', 113.4, detour=detour)
    invalid.options = [transit(duration=100, fare=1, lon=113.4)]
    original = invalid.model_dump()
    result = rank_recommendations([invalid, valid], Preferences(modes=modes, max_detour_minutes=5, priority='cost'))
    assert [r.id for r in result] == ['valid']
    assert invalid.model_dump() == original


@pytest.mark.parametrize('shared,kept', [(2699, True), (2700, False)])
def test_severe_overshoot_filter_after_three_good_alternatives(shared, kept):
    good = [rec(str(i), 113.3 + i * .04, detour=300 - i * 60, fare=20 + i * 5) for i in range(3)]
    outlier = rec('outlier', 113.5, fare=80, shared=shared)
    result = rank_recommendations([outlier, *good], Preferences(modes=['taxi']))
    assert ('outlier' in {r.id for r in result}) is kept


@pytest.mark.parametrize('has_verified_transit', [False, True])
def test_nearby_same_name_cards_keep_routes_verified_at_representative_point(has_verified_transit):
    primary = rec('station', 113.3, fare=12)
    if has_verified_transit:
        primary.options.append(transit())
    nearby = rec('station', 113.301)
    nearby.id = 'nearby'
    nearby.options = [transit(duration=300, fare=2, lon=113.301)]
    result = rank_recommendations([primary, nearby], Preferences())
    assert len(result) == (1 if has_verified_transit else 2)
    if has_verified_transit:
        assert result[0].point.point == primary.point.point
        option = next(o for o in result[0].options if o.mode == 'transit')
        assert option.duration_s == 900
        assert option.boarding_stop.point == primary.point.point


def test_interleaved_cards_have_sequential_labels_and_matching_reason_prefixes():
    taxi = rec('taxi', 113.3)
    subway = rec('subway', 113.4)
    subway.options = [transit(duration=300, lon=113.4)]
    result = rank_recommendations([taxi, subway], Preferences())
    assert {r.suggested_mode for r in result} == {'taxi', 'transit'}
    for r, label in zip(result, ['综合推荐', '综合备选2']):
        assert r.recommendation_label == label
        assert r.reason.startswith(label + '：')

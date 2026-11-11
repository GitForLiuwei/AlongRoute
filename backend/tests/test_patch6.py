"""Behavioral coverage for the sixth attached update."""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.amap import AmapClient, LOCAL_TZ, calculate_live
from app.candidates import Candidate
from app.hub_discovery import HubDiscovery, _highway_gate_points
from app.hub_store import HubStore
from app.models import ModeOption, Place, Point, Preferences, RecommendationRequest, RoadSegment, Route
from app.optimizer import taxi_score_seconds


def point(lon, lat=23.1):
    return Point(lon=lon, lat=lat)


@pytest.fixture
def planning(monkeypatch):
    """One verified roadside drop-off, with all map requests kept offline."""
    a, b, p, c = point(113), point(114), point(113.5), point(113.504)
    req = RecommendationRequest(origin=Place(name='A', point=a), driver_destination=Place(name='B', point=b),
        passenger_destination=Place(name='C', point=c), preferences=Preferences(modes=['taxi']))
    calls = []
    walk = [Route(distance_m=600, duration_s=480, polyline=[p, c], walk_instructions=['沿普通道路向东步行'])]
    candidate = Candidate(Place(name='P', point=p), 'roadside', 0, None, 0, region='hub:0:w')

    async def discover(self, *args):
        self.candidates = [candidate]
        return self

    async def driving(self, origin, destination, via=None):
        calls.append(('driving', origin, destination, via))
        points = [origin, *([via] if via is not None else []), destination]
        return Route(duration_s=700 if via is not None else 600, distance_m=20000, polyline=points,
            fare_yuan=20, dropoff_duration_s=350 if via is not None else None,
            segments=[RoadSegment(name='普通道路', polyline=points)])

    async def walking(self, origin, destination):
        calls.append(('walking', origin, destination))
        return walk[0]

    async def transit(self, origin, destination, at, prefs):
        calls.append(('transit', origin.point, destination.point))
        return [ModeOption(mode='transit', duration_s=600, fare_yuan=2, walking_m=100,
            transfers=0, boarding_walk_m=100, boarding_stop=origin, boarding_kind='subway', details='地铁方案')]

    monkeypatch.setenv('AMAP_WEB_SERVICE_KEY', 'offline')
    monkeypatch.setattr(HubDiscovery, 'run', discover)
    monkeypatch.setattr(AmapClient, 'driving', driving)
    monkeypatch.setattr(AmapClient, 'walking', walking)
    monkeypatch.setattr(AmapClient, 'transit', transit)
    return req, walk, calls


@pytest.mark.parametrize('modes', [['taxi'], ['transit'], ['taxi', 'transit']])
def test_direct_walk_uses_verified_route_and_updates_arrival(planning, modes):
    req, walk, calls = planning
    req.preferences.modes = modes
    req.preferences.max_taxi_fare_yuan = 0
    response = asyncio.run(calculate_live(req))
    assert len(response.recommendations) == 1
    rec = response.recommendations[0]
    assert rec.recommendation_label == '¥0 · 直接步行回家'
    assert len(rec.options) == 1
    option = rec.options[0]
    assert option.direct_walk and option.fare_yuan == 0
    assert option.route == walk[0].polyline
    assert option.wait_estimate_s == 0 and option.boarding_stop is None
    assert option.walking_m == 600 and option.duration_s == 480
    assert option.total_duration_s == 350 + 90 + 480
    assert option.arrival_at == response.departure_at + timedelta(seconds=option.total_duration_s)
    assert '末班车' not in option.details
    assert ('walking', rec.point.point, req.passenger_destination.point) in calls


@pytest.mark.parametrize('walk_distance,limit,accepted', [(1200, 1200, True), (1201, 1200, False), (600, 599, False)])
def test_direct_walk_respects_route_length_and_user_limit(planning, walk_distance, limit, accepted):
    req, walk, calls = planning
    req.preferences.max_walking_m = limit
    walk[0] = walk[0].model_copy(update={'distance_m': walk_distance})
    response = asyncio.run(calculate_live(req))
    option = response.recommendations[0].options[0]
    assert option.direct_walk is accepted
    assert option.fare_yuan == (0 if accepted else 20)


def test_missing_walk_route_keeps_original_taxi(planning):
    req, walk, _ = planning
    walk[0] = None
    response = asyncio.run(calculate_live(req))
    option = response.recommendations[0].options[0]
    assert not option.direct_walk and option.fare_yuan == 20
    assert option.wait_estimate_s == 300


@pytest.mark.parametrize('minute,warn', [(22*60+14, False), (22*60+15, True), (23*60+59, True),
                                        (0, True), (5*60+49, True), (5*60+50, False), (6*60, False)])
def test_last_train_warning_uses_dropoff_arrival_in_china_time(planning, minute, warn):
    req, walk, _ = planning
    req.preferences.modes = ['transit']
    walk[0] = None
    tomorrow = (datetime.now(LOCAL_TZ) + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    target = tomorrow + timedelta(minutes=minute)
    req.departure_time = (target - timedelta(seconds=350 + 90)).astimezone(timezone.utc)
    response = asyncio.run(calculate_live(req))
    option = response.recommendations[0].options[0]
    assert ('末班车' in option.details) is warn
    if warn:
        assert target.strftime('%H:%M') in option.details


@pytest.mark.parametrize('action', ['前方掉头', '前方调头'])
def test_forward_carriageway_before_uturn_is_first_candidate(action):
    a, before, p = point(113), point(113.5005), point(113.5)
    route = Route(duration_s=600, distance_m=50000, polyline=[a, before, p], segments=[
        RoadSegment(name='普通道路', polyline=[a, before]),
        RoadSegment(name='普通道路', instruction=action, polyline=[before, p])])

    class API:
        async def driving(self, *args):
            return route

    discovery = HubDiscovery()
    discovery.api = API()
    discovery.request = RecommendationRequest(origin=Place(name='A', point=a), driver_destination=Place(name='B', point=point(114)),
        passenger_destination=Place(name='C', point=point(113.6)), preferences=Preferences(modes=['transit']))
    asyncio.run(discovery.approach(Candidate(Place(name='站口', point=p), 'station_area', 0, None, 0,
                                            hub_kind='subway', region='hub:0:w')))
    assert discovery.approached[0].place.point == before


def test_subway_core_seeds_cover_both_sides_of_center():
    center = point(113.5)
    west, east = Place(name='西站A口', point=point(113.48)), Place(name='东站B口', point=point(113.52))

    class API:
        key = 'offline'
        async def nearby(self, *args, **kwargs):
            return [west, east]

    discovery = HubDiscovery()
    discovery.api = API()
    discovery.request = RecommendationRequest(origin=Place(name='A', point=point(113)),
        driver_destination=Place(name='B', point=point(114)), passenger_destination=Place(name='C', point=point(113.6)))
    asyncio.run(discovery.fetch(center, '地铁站', 'subway', 0))
    assert {c.region for c in discovery.seeds.values()} == {'hub:0:w', 'hub:0:e'}


def test_gate_recall_includes_named_interchanges_and_toll_transitions():
    a, gate, end = point(113), point(113.5), point(114)
    route = Route(duration_s=600, distance_m=100000, polyline=[a, gate, end], segments=[
        RoadSegment(name='G15高速', polyline=[a, gate]),
        RoadSegment(name='清湖立交', instruction='经过收费站', polyline=[gate, end])])
    assert gate in _highway_gate_points(route)


def test_static_hub_pool_defaults_to_thirty_days():
    assert HubStore().ttl == 30 * 24 * 3600


def test_detour_penalty_begins_above_eighteen_minutes():
    option = ModeOption(mode='taxi', duration_s=600, total_duration_s=1000, fare_yuan=20, details='taxi')
    assert taxi_score_seconds(1080, option) - taxi_score_seconds(1079, option) == Decimal('1.5')
    assert taxi_score_seconds(1081, option) - taxi_score_seconds(1080, option) == Decimal('2.625')


def test_large_detour_pruning_saves_onward_requests_and_reports_incomplete_search(monkeypatch):
    a, b, c = point(113), point(114.8), point(114.6)
    candidates = [Candidate(Place(name=f'P{i}', point=point(113.1 + i * .04)),
                            'roadside', 0, None, 0, region=f'hub:{i // 8}') for i in range(9)]
    onward_points = []
    req = RecommendationRequest(origin=Place(name='A', point=a), driver_destination=Place(name='B', point=b),
        passenger_destination=Place(name='C', point=c), preferences=Preferences(modes=['taxi']))

    async def discover(self, *args):
        self.candidates = candidates
        self.complete = True
        return self

    async def driving(self, origin, destination, via=None):
        points = [origin, *([via] if via is not None else []), destination]
        if via is not None:
            index = next(i for i, candidate in enumerate(candidates) if candidate.place.point == via)
            extra = 4000 if index == 8 else 100 + index * 100
            return Route(duration_s=7200 + extra, distance_m=200000, polyline=points,
                dropoff_duration_s=6500-index*250, segments=[RoadSegment(name='普通道路', polyline=points)])
        if origin in [candidate.place.point for candidate in candidates]:
            onward_points.append(origin)
            index = next(i for i, candidate in enumerate(candidates) if candidate.place.point == origin)
            fare = 20-index*2
        else:
            fare = 40
        return Route(duration_s=7200 if origin == a and destination == b else 600,
            distance_m=200000, fare_yuan=fare, polyline=points,
            segments=[RoadSegment(name='普通道路', polyline=points)])

    monkeypatch.setenv('AMAP_WEB_SERVICE_KEY', 'offline')
    monkeypatch.setattr(HubDiscovery, 'run', discover)
    monkeypatch.setattr(AmapClient, 'driving', driving)
    response = asyncio.run(calculate_live(req))
    assert len(response.recommendations) == 6
    assert candidates[-1].place.point not in onward_points
    assert len(onward_points) == 8
    assert response.examined_candidates == 9
    assert not response.search_complete
    assert any('提前停止接驳查询' in warning for warning in response.warnings)

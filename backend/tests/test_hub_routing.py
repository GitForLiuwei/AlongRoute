import asyncio

import httpx
import pytest

from app.amap import AmapClient, MapServiceError
from app.candidates import Candidate
from app.coarse_routing import estimate_candidates
from app.coordinates import gcj02_to_wgs84, wgs84_to_gcj02
from app.geometry import haversine_m
from app.hub_discovery import HubDiscovery, recall_centers
from app.hub_store import HubStore
from app.map_cache import MapResponseCache, TTL_SECONDS
from app.models import ModeOption, Place, Point, Preferences, Recommendation, RecommendationRequest, RoadSegment, Route
from app.optimizer import rank_recommendations
from app.osrm import OsrmClient


A = Point(lon=113.3, lat=23.1)
B = Point(lon=113.5, lat=23.1)
C = Point(lon=113.45, lat=23.13)
P = Point(lon=113.4, lat=23.1)
Q = Point(lon=113.42, lat=23.1)


def request():
    return RecommendationRequest(origin=Place(name='A', point=A, citycode='020'),
        driver_destination=Place(name='B', point=B, citycode='020'),
        passenger_destination=Place(name='C', point=C, citycode='020'),
        preferences=Preferences(max_detour_minutes=5))


def road(a=A, b=B):
    return Route(duration_s=600, distance_m=20000, polyline=[a,b],
                 segments=[RoadSegment(name='普通道路', polyline=[a,b])])


def matrix_transport(change=None):
    calls = []
    def respond(req):
        calls.append(req)
        raw = req.url.path.split('/driving/')[1]
        coords = [[float(v) for v in pair.split(',')] for pair in raw.split(';')]
        sources = [int(v) for v in req.url.params['sources'].split(';')]
        dests = [int(v) for v in req.url.params['destinations'].split(';')]
        if sources == [0]:
            durations = [[600,400,500]]
        else:
            durations = [[350,200],[250,100]]
        data = dict(code='Ok', sources=[dict(location=coords[i]) for i in sources],
                    destinations=[dict(location=coords[i]) for i in dests], durations=durations,
                    distances=[[x*10 for x in row] for row in durations])
        if change:
            change(data, sources)
        return httpx.Response(200, json=data)
    return httpx.MockTransport(respond), calls


def run_matrix(change=None):
    transport, calls = matrix_transport(change)
    async def query():
        async with httpx.AsyncClient(transport=transport) as client:
            return await OsrmClient(client, 'http://osrm').estimate(A,B,C,[P,Q])
    return asyncio.run(query()), calls


def test_directed_sparse_matrix_and_coordinate_boundary():
    values, calls = run_matrix()
    assert len(calls) == 2
    assert values[0].detour_s == 150
    assert values[0].shared_s == 400
    assert values[0].onward_s == 200 and values[0].onward_m == 2000
    assert values[1].detour_s == 150 and values[1].onward_s == 100
    assert sorted(req.url.params['sources'] for req in calls) == ['0','3;4']
    assert A.csv() not in str(calls[0].url)  # GCJ-02 must not leak into OSM queries.
    assert all('fallback_speed' not in r.url.params for r in calls)


def test_missing_cell_does_not_erase_other_candidates():
    def change(data, sources):
        if sources == [0]:
            data['durations'][0][1] = None
    values, _ = run_matrix(change)
    assert values[0].missing_reason == 'missing_cell'
    assert values[1].onward_m == 1000


@pytest.mark.parametrize('failure', ['shape','snap','fallback','unsupported'])
def test_unusable_matrix_is_only_an_estimation_failure(failure):
    def change(data, sources):
        if failure == 'shape':
            data['distances'] = []
        elif failure == 'snap':
            data['sources'][0]['location'][0] += .1
        elif failure == 'fallback':
            data['fallback_speed_cells'] = [[0,0]]
        else:
            data['code'] = 'NotImplemented'
    values, _ = run_matrix(change)
    assert all(v.missing_reason for v in values)


def test_matrix_timeout_is_bounded_and_cancels_requests():
    cancelled = []
    async def respond(req):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(req)
    async def query():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await OsrmClient(client,'http://osrm',timeout=.01).estimate(A,B,C,[P,Q])
    assert all(v.missing_reason for v in asyncio.run(query()))
    assert len(cancelled) == 2


def test_conversion_roundtrip_and_outside_china():
    original = Point(lon=113.2644, lat=23.1291)
    shifted = wgs84_to_gcj02(original)
    assert haversine_m(original, shifted) > 100
    assert haversine_m(original, gcj02_to_wgs84(shifted)) < .1
    paris = Point(lon=2.35, lat=48.85)
    assert gcj02_to_wgs84(paris) == paris


def test_estimator_preserves_large_detour_and_missing_candidates(monkeypatch):
    monkeypatch.setenv('OSRM_BASE_URL','http://osrm')
    transport, _ = matrix_transport(lambda data, sources: data['durations'][0].__setitem__(1, 10000) if sources == [0] else None)
    candidates = [Candidate(Place(name=str(i),point=p),'roadside',0,None,0) for i,p in enumerate([P,Q])]
    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            return await estimate_candidates(AmapClient('test',client),request(),[road()],candidates)
    ordered = asyncio.run(run())
    assert len(ordered) == 2
    assert candidates[0].coarse['detour_s'] > request().preferences.max_detour_minutes*60
    assert candidates[0] in ordered


def test_hub_store_spatial_lookup_empty_results_and_namespace(tmp_path, monkeypatch):
    now = [1000]
    monkeypatch.setattr('app.hub_store.time',lambda: now[0])
    store = HubStore(tmp_path/'hubs.sqlite3',ttl=10)
    place = Place(name='地铁入口', point=P)
    store.put('key1', P, 'subway',5000,[place])
    assert store.lookup('key1',P,'subway',5000) == ([place],True)
    assert store.lookup('key2',P,'subway',5000) == ([],False)
    assert store.lookup('key1',A,'subway',100) == ([],False)
    store.put('key1',P,'bus',5000,[])
    assert store.lookup('key1',P,'bus',5000) == ([],True)
    now[0] += 11
    assert store.lookup('key1',P,'subway',5000) == ([],False)


def test_hub_first_finds_station_off_corridor_before_exit_search(monkeypatch):
    hubs = [Place(name=f'枢纽{i}',point=Point(lon=113.4 + i*.015,lat=23.115),citycode='020') for i in range(6)]
    seen = []
    async def nearby(self, center, keyword, radius=None):
        assert radius == 5000
        return hubs if keyword == '地铁站' else []
    async def driving(self, origin, destination, via=None):
        seen.append(destination)
        return road(origin,destination)
    async def forbidden(*args, **kwargs):
        raise AssertionError('Healthy hub coverage must not start the old exit search')
    monkeypatch.setattr(AmapClient,'nearby',nearby)
    monkeypatch.setattr(AmapClient,'driving',driving)
    monkeypatch.setattr('app.hub_discovery.FocusedDiscovery.run',forbidden)
    async def run():
        async with httpx.AsyncClient() as client:
            baseline = road()
            baseline.segments[0].name = 'G4高速'
            return await HubDiscovery().run(AmapClient('test',client),request(),[baseline])
    result = asyncio.run(run())
    station_candidates = [c for c in result.candidates if c.source == 'station_area']
    assert len(station_candidates) >= 8
    assert any(haversine_m(c.place.point, Point(lon=c.place.point.lon,lat=A.lat)) > 1000 for c in station_candidates)
    assert all(c.place.point not in [h.point for h in hubs] for c in station_candidates)
    assert all(c.discovery_group == 'hub' for c in station_candidates)
    assert result.complete is False


def test_bad_approach_does_not_turn_station_center_into_dropoff(monkeypatch):
    async def nearby(self,center,keyword,radius=None):
        return [Place(name='某地铁站',point=P)]
    async def driving(self,origin,destination,via=None):
        r = road(origin,destination)
        r.segments[0].name = '高速公路'
        return r
    monkeypatch.setattr(AmapClient,'nearby',nearby)
    monkeypatch.setattr(AmapClient,'driving',driving)
    async def run():
        async with httpx.AsyncClient() as client:
            return await HubDiscovery().run(AmapClient('test',client),request(),[road()])
    result = asyncio.run(run())
    assert result.candidates  # Ordinary road supplement remains.
    assert all(c.discovery_group == 'hub_surface' for c in result.candidates)


def test_cached_hub_survives_provider_failure(monkeypatch, tmp_path):
    from hashlib import sha256
    store = HubStore(tmp_path/'hubs.sqlite3')
    store.put(sha256(b'test').hexdigest(),P,'subway',1000,[Place(name='cached',point=P)])
    monkeypatch.setattr('app.hub_discovery.hub_store',store)
    async def fail(*args,**kwargs):
        raise MapServiceError('failed')
    async def run():
        async with httpx.AsyncClient() as client:
            d = HubDiscovery()
            d.api,d.request = AmapClient('test',client),request()
            d.api.nearby = fail
            await d.fetch(P,'地铁站','subway',0)
            return d
    result = asyncio.run(run())
    assert result.failed == 1 and len(result.seeds) == 1


def test_empty_hubs_use_bounded_exit_supplement(monkeypatch):
    async def nearby(*args, **kwargs):
        return []
    async def driving(self, origin, destination, via=None):
        return road(origin, destination)
    async def supplement(self, api, req, routes):
        assert self.discovery_budget_s <= 8
        assert self.include_stations is False and self.query_limit == 6
        assert self.passenger_routes is not None
        self.candidates = [Candidate(Place(name='出口外道路',point=P),'roadside',0,None,0,
            exit_place=Place(name='出口',point=Q),approach=A,modes=('taxi',),region='exit')]
        return self
    monkeypatch.setattr(AmapClient,'nearby',nearby)
    monkeypatch.setattr(AmapClient,'driving',driving)
    monkeypatch.setattr('app.hub_discovery.FocusedDiscovery.run',supplement)
    async def run():
        async with httpx.AsyncClient() as client:
            baseline = road()
            baseline.segments[0].name = '高速公路'
            return await HubDiscovery().run(AmapClient('test',client),request(),[baseline])
    result = asyncio.run(run())
    assert any(c.exit_place and c.approach == A for c in result.candidates)


def test_cached_and_fresh_duplicates_do_not_spend_seed_quota(monkeypatch, tmp_path):
    from hashlib import sha256
    store = HubStore(tmp_path/'hubs.sqlite3')
    places = [Place(name=f'P{i}',point=Point(lon=P.lon+i*.0001,lat=P.lat)) for i in range(25)]
    store.put(sha256(b'test').hexdigest(),P,'subway',1000,places)
    monkeypatch.setattr('app.hub_discovery.hub_store',store)
    async def nearby(*args,**kwargs):
        return places
    async def run():
        async with httpx.AsyncClient() as client:
            d = HubDiscovery()
            d.api,d.request = AmapClient('test',client),request()
            d.api.nearby = nearby
            await d.fetch(P,'地铁站','subway',0)
            return d
    assert len(asyncio.run(run()).seeds) == 25


def test_cache_policy_invalidates_old_driving_entries(monkeypatch):
    new = MapResponseCache.key('key','/v5/direction/driving',{'origin':A.csv()})
    assert TTL_SECONDS['/v5/direction/driving'] == 180
    assert TTL_SECONDS['/v5/direction/transit/integrated'] == 300
    monkeypatch.setitem(TTL_SECONDS,'/v5/direction/driving',86400)
    assert new != MapResponseCache.key('key','/v5/direction/driving',{'origin':A.csv()})


def test_same_point_different_driver_trip_does_not_merge_modes():
    taxi = Recommendation(id='taxi',point=Place(name='P',point=P),driver_detour_s=90,
        shared_ride_s=100,driver_route=road(),suggested_mode='taxi',score=0,reason='test',
        options=[ModeOption(mode='taxi',duration_s=100,fare_yuan=12,details='taxi')])
    transit = taxi.model_copy(deep=True)
    transit.id,transit.shared_ride_s = 'transit',500
    transit.options = [ModeOption(mode='transit',duration_s=100,fare_yuan=2,walking_m=100,transfers=0,
        boarding_walk_m=100,boarding_stop=Place(name='入口',point=P),details='subway')]
    result = rank_recommendations([taxi,transit],Preferences())
    assert len(result) == 2
    assert all(len(r.options) == 1 for r in result)
    assert {r.options[0].total_duration_s for r in result} == {290,690}

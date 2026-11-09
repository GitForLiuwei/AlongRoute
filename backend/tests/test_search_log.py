import asyncio
import json

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import search_log
from app.amap import MapServiceError
from app.main import app
from app.models import Preferences
from app.optimizer import rank_recommendations
from test_taxi_optimizer import candidate


@pytest.fixture
def logs(monkeypatch, tmp_path):
    monkeypatch.setattr(search_log, "LOG_DIR", tmp_path)
    monkeypatch.delenv("AMAP_WEB_SERVICE_KEY", raising=False)
    return tmp_path


def payload():
    return dict(origin=dict(name="共同出发地", point=dict(lon=113.3, lat=23.1)),
                driver_destination=dict(name="司机终点", point=dict(lon=113.4, lat=23.1)),
                passenger_destination=dict(name="乘客终点", point=dict(lon=113.35, lat=23.2)),
                preferences=dict(modes=["taxi"], max_detour_minutes=5))


def records(directory):
    return [json.loads(line) for path in directory.glob("*.jsonl") for line in path.read_text(encoding="utf-8").splitlines()]


def test_successful_search_links_request_result_and_timing(logs):
    with TestClient(app) as client:
        response = client.post("/api/recommend", json=payload())
    assert response.status_code == 200
    data = records(logs)
    identifier = response.json()["search_id"]
    assert identifier == response.headers["X-Search-ID"]
    assert {record["search_id"] for record in data} == {identifier}
    assert data[0]["event"] == "search_started"
    assert data[-1]["event"] == "search_finished"
    assert data[-1]["status"] == "success" and data[-1]["elapsed_ms"] >= 0
    assert next(r for r in data if r["event"] == "route_input")["request"] == {
        **payload(), "departure_time": None,
        "origin": {**payload()["origin"], "citycode": None, "poi_id": None},
        "driver_destination": {**payload()["driver_destination"], "citycode": None, "poi_id": None},
        "passenger_destination": {**payload()["passenger_destination"], "citycode": None, "poi_id": None},
        "preferences": {**payload()["preferences"], "priority": "balanced", "max_walking_m": 1200,
                        "max_transfers": 2, "max_taxi_fare_yuan": 200}}
    result = next(r for r in data if r["event"] == "route_result")
    assert result["recommendations"] and result["baseline"]["distance_m"] > 0
    assert "driver_route" not in result["recommendations"][0]  # no duplicated raw geometry


def test_live_hub_search_with_http_logging_and_replay(logs, monkeypatch):
    from app.amap import AmapClient
    from app.models import Place, Point, RoadSegment, Route
    monkeypatch.setenv('AMAP_WEB_SERVICE_KEY', 'offline-test-key')
    async def driving(self, origin, destination, via=None):
        points = [origin, *([via] if via else []), destination]
        route = Route(duration_s=700 if via else 600, distance_m=10000,
            polyline=points, dropoff_duration_s=350 if via else None, fare_yuan=12,
            segments=[RoadSegment(name='普通道路', polyline=points)])
        self.driving_choices = [route]
        return route
    async def nearby(self, center, keyword, radius=None):
        return [Place(name='枢纽停车场', point=Point(lon=113.35, lat=23.1))] if keyword == '停车场' else []
    monkeypatch.setattr(AmapClient, 'driving', driving)
    monkeypatch.setattr(AmapClient, 'nearby', nearby)
    with TestClient(app) as client:
        response = client.post('/api/recommend', json=payload())
        assert response.status_code == 200, response.text
        assert response.json()['recommendations']
        replay = client.get('/api/search-replay/' + response.json()['search_id'])
    data = records(logs)
    hubs = [r for r in data if r['event'] == 'hub_seeds']
    assert hubs and any(r['hub_kind'] == 'parking' for r in hubs)
    assert all(r['kind'] == 'route' for r in data)
    assert data[-1]['status'] == 'success'
    assert replay.status_code == 200
    assert any(r['event'] == 'hub_seeds' and r['hub_kind'] == 'parking' for r in replay.json()['events'])


def test_log_payload_cannot_overwrite_request_metadata(logs):
    trace, token = search_log.start_trace('route')
    try:
        search_log.event('diagnostic', kind='subway', search_id='wrong', sequence=-1)
    finally:
        search_log.reset_trace(token)
    record = records(logs)[0]
    assert record['kind'] == 'route' and record['search_id'] == trace.id
    assert record['sequence'] == 1


def test_place_search_and_validation_failures_are_logged(logs):
    with TestClient(app) as client:
        place = client.get("/api/places/suggest", params={"q": "广州塔"})
        invalid = client.post("/api/recommend", json={"origin": {"name": "invalid"}})
        client.get("/api/health")
    data = records(logs)
    assert place.status_code == 200 and invalid.status_code == 422
    assert len({r["search_id"] for r in data}) == 2
    assert next(r for r in data if r["event"] == "place_query")["query"] == "广州塔"
    assert any(r["event"] == "search_finished" and r["http_status"] == 422 for r in data)
    assert any(r["event"] == "validation_failed" and r["errors"] for r in data)


@pytest.mark.parametrize("status", [499, 502, 504])
def test_failed_cancelled_and_timed_out_searches_are_logged(logs, monkeypatch, status):
    monkeypatch.setenv("AMAP_WEB_SERVICE_KEY", "secret-service-key")
    async def failing(request):
        if status == 502:
            raise MapServiceError("map failed key=secret-service-key&jscode=another-secret")
        raise HTTPException(status, "stopped")
    monkeypatch.setattr("app.main.calculate_live", failing)
    with TestClient(app) as client:
        response = client.post("/api/recommend", json=payload())
    data = records(logs)
    assert response.status_code == status
    assert data[-1]["status"] == {499: "cancelled", 502: "error", 504: "timeout"}[status]
    text = json.dumps(data)
    assert "secret-service-key" not in text and "another-secret" not in text


def test_concurrent_searches_keep_independent_identifiers_and_inputs(logs):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await asyncio.gather(*(client.get("/api/places/suggest", params={"q": query})
                                          for query in ("广州塔", "广州南站")))
    responses = asyncio.run(run())
    ids = [response.headers["X-Search-ID"] for response in responses]
    assert len(set(ids)) == 2
    data = records(logs)
    for identifier, query in zip(ids, ("广州塔", "广州南站")):
        scoped = [r for r in data if r["search_id"] == identifier]
        assert [r["query"] for r in scoped if r["event"] == "place_query"] == [query]
        assert scoped[0]["sequence"] == 1 and scoped[-1]["event"] == "search_finished"


def test_filter_log_names_the_candidate_that_replaces_expensive_option(logs):
    trace, token = search_log.start_trace("route")
    try:
        rank_recommendations([candidate("expensive", 130, 199), candidate("useful", 100, 111)],
                             Preferences(modes=["taxi"]))
    finally:
        search_log.reset_trace(token)
    filtered = next(r for r in records(logs) if r["event"] == "taxi_filtered")
    assert filtered["search_id"] == trace.id
    assert filtered["reason"] == "dominated"
    assert filtered["candidate_id"] == "expensive" and filtered["by_candidate_id"] == "useful"
    assert filtered["fare_yuan"] == 199 and filtered["passenger_total_s"] == 1290


def test_log_write_failure_does_not_break_search(logs, monkeypatch):
    def unavailable(path):
        raise OSError("disk unavailable")
    monkeypatch.setattr(search_log, "_logger", unavailable)
    with TestClient(app) as client:
        response = client.post("/api/recommend", json=payload())
    assert response.status_code == 200
    assert response.headers["X-Search-Log-Status"] == "unavailable"
    assert any("日志无法写入" in warning for warning in response.json()["warnings"])

import asyncio

import httpx
import pytest

from app.amap import AmapClient, MapServiceError, RequestGate, calculate_live
from app.models import Place, Point, RecommendationRequest, Route


def test_place_search_preserves_transit_citycode():
    def respond(request):
        assert request.url.path == "/v5/place/text"
        assert request.url.params["keywords"] == "广州塔"
        assert request.url.params["page_size"] == "8"
        return httpx.Response(200, json={
            "status": "1", "pois": [
                {"id": "poi-1", "name": "广州塔", "address": "阅江西路",
                 "location": "113.3245,23.1065", "citycode": "020"},
                {"name": "No coordinates", "location": []},
                {"name": "No city", "location": "113.3,23.1", "citycode": []},
            ]
        })

    async def search():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await AmapClient("test-key", client).suggest("广州塔")

    places = asyncio.run(search())
    assert len(places) == 2
    assert places[0].citycode == "020"
    assert places[0].poi_id == "poi-1"
    assert places[0].point.lon == 113.3245
    assert places[1].citycode is None


def test_place_search_reports_provider_failure():
    async def search():
        transport = httpx.MockTransport(lambda request: httpx.Response(
            200, json={"status": "0", "info": "INVALID_USER_KEY"}))
        async with httpx.AsyncClient(transport=transport) as client:
            return await AmapClient("test-key", client).suggest("广州塔")

    with pytest.raises(MapServiceError, match="INVALID_USER_KEY"):
        asyncio.run(search())


@pytest.mark.parametrize("always_limited", [False, True])
def test_qps_errors_retry_with_a_bound(monkeypatch, always_limited):
    attempts = []
    delays = []

    async def no_gate(path):
        pass

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("app.amap.wait_for_request_slot", no_gate)
    monkeypatch.setattr("app.amap.asyncio.sleep", fake_sleep)

    def respond(request):
        attempts.append(request)
        if always_limited or len(attempts) == 1:
            return httpx.Response(200, json={
                "status": "0", "infocode": "10021",
                "info": "CUQPS_HAS_EXCEEDED_THE_LIMIT"})
        return httpx.Response(200, json={"status": "1", "pois": []})

    async def search():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await AmapClient("test-key", client).suggest("广州塔")

    if always_limited:
        with pytest.raises(MapServiceError, match="频率受限"):
            asyncio.run(search())
        assert len(attempts) == 3
        assert delays == [1, 2]
    else:
        assert asyncio.run(search()) == []
        assert len(attempts) == 2
        assert delays == [1]


def test_request_gate_spaces_concurrent_starts():
    async def measure():
        gate = RequestGate(interval=0.02)

        async def start():
            await gate.wait()
            return asyncio.get_running_loop().time()

        return await asyncio.gather(*(start() for _ in range(3)))

    starts = asyncio.run(measure())
    assert all(right - left >= 0.015 for left, right in zip(starts, starts[1:]))


@pytest.mark.parametrize("failed_stage", ["search", "evaluation"])
def test_provider_failures_are_not_reported_as_no_candidates(monkeypatch, failed_stage):
    request = RecommendationRequest(
        origin=Place(name="A", point=Point(lon=113.3, lat=23.1)),
        driver_destination=Place(name="B", point=Point(lon=113.4, lat=23.2)),
        passenger_destination=Place(name="C", point=Point(lon=113.5, lat=23.3)),
    )

    async def driving(self, origin, destination, via=None):
        if origin != request.origin.point or destination != request.driver_destination.point:
            raise MapServiceError("路线查询失败")
        return Route(duration_s=600, distance_m=1000, polyline=[origin, destination])

    async def nearby(self, center, keyword):
        if failed_stage == "search":
            raise MapServiceError("地点查询失败")
        return [Place(name="P", point=Point(lon=113.35, lat=23.15), poi_id="stop-1")]

    monkeypatch.setenv("AMAP_WEB_SERVICE_KEY", "test-key")
    monkeypatch.setattr(AmapClient, "driving", driving)
    monkeypatch.setattr(AmapClient, "nearby", nearby)
    with pytest.raises(MapServiceError, match="查询失败"):
        asyncio.run(calculate_live(request))

import asyncio
from datetime import datetime, timezone

import httpx
from fastapi.testclient import TestClient

from app.amap import AmapClient
from app.main import app
from app.models import Place, Point, Preferences, Route
from app.transit import parse_itinerary


def itinerary():
    return {"cost": {"duration": "1800", "transit_fee": "5"}, "walking_distance": "600", "segments": [
        {"walking": {"distance": "100", "cost": {"duration": "100"}, "steps": [{"instruction": "原接驳步行指引"}]},
         "bus": {"buslines": [{"name": "M123路(始发站--公交终点)", "type": "普通公交", "distance": "1500",
            "cost": {"duration": "300"}, "departure_stop": {"name": "公交甲站", "location": "113.311,23.1"},
            "arrival_stop": {"name": "换乘乙站"}, "via_num": "0", "via_stops": []}]}},
        {"walking": {"distance": "200", "cost": {"duration": "200"}, "steps": [{"instruction": "经人行通道进入B口"}]},
         "bus": {"buslines": [{"name": "地铁2号线(西端--东端)", "type": "地铁线路", "distance": "6000",
            "cost": {"duration": "600"}, "departure_stop": {"name": "换乘乙站", "entrance": {"name": "B口"}},
            "arrival_stop": {"name": "地铁丙站", "exit": {"name": "D口"}}, "via_num": "2",
            "via_stops": [{"name": "中间一站"}, {"name": "中间二站"}]}]}},
        {"walking": {"distance": "300", "cost": {"duration": "300"}, "steps": [{"instruction": "出站后沿人行道到目的地"}]}}]}


def test_bus_metro_transfer_and_final_walk_remain_in_order():
    option = parse_itinerary(itinerary(), "020")
    assert [step.kind for step in option.transit_steps] == ["walk", "bus", "walk", "subway", "walk"]
    first, bus, transfer, metro, last = option.transit_steps
    assert (first.from_stop, first.to_stop, first.distance_m) == ("下车点", "公交甲站", 100)
    assert (bus.line_name, bus.direction, bus.from_stop, bus.to_stop) == ("M123路", "公交终点", "公交甲站", "换乘乙站")
    assert bus.stop_count == 1  # via_num excludes the boarding and alighting stops.
    assert transfer.title == "步行换乘" and transfer.to_stop == "换乘乙站B口"
    assert metro.title == "换乘" and metro.direction == "东端" and metro.stop_count == 3
    assert metro.entrance == "B口" and metro.exit == "D口"
    assert metro.via_stops == ["中间一站", "中间二站"]
    assert last.from_stop == "地铁丙站" and last.to_stop == "目的地" and last.distance_m == 300
    assert last.instructions == ["出站后沿人行道到目的地"]


def test_unknown_direction_or_stop_count_are_not_invented():
    item = itinerary()
    bus = item["segments"][0]["bus"]["buslines"][0]
    bus["name"] = "地铁接驳专线"
    bus.pop("via_num")
    option = parse_itinerary(item, "020")
    step = option.transit_steps[1]
    assert step.kind == "bus" and step.direction is None and step.stop_count is None


def test_explicit_line_endpoint_takes_precedence_over_name():
    item = itinerary()
    bus = item["segments"][0]["bus"]["buslines"][0]
    bus["end_stop"] = "终点更新站"
    assert parse_itinerary(item, "020").transit_steps[1].direction == "终点更新站"


def test_verified_first_walk_replaces_distance_time_and_instructions(monkeypatch):
    async def get(self, path, **params):
        return {"route": {"transits": [itinerary()]}}
    async def walking(self, origin, destination):
        return Route(duration_s=100, distance_m=250, polyline=[origin, destination],
                     walk_instructions=["核验后：沿同侧人行道前往公交甲站"])
    monkeypatch.setattr(AmapClient, "get", get)
    monkeypatch.setattr(AmapClient, "walking", walking)
    async def run():
        async with httpx.AsyncClient() as client:
            return await AmapClient("test", client).transit(
                Place(name="P", point=Point(lon=113.31, lat=23.1), citycode="020"),
                Place(name="C", point=Point(lon=113.33, lat=23.1), citycode="020"),
                datetime.now(timezone.utc), Preferences())
    option = asyncio.run(run())[0]
    assert option.walking_m == 750
    assert option.transit_steps[0].distance_m == option.boarding_walk_m == 250
    assert option.transit_steps[0].duration_s == 100
    assert option.transit_steps[0].instructions == ["核验后：沿同侧人行道前往公交甲站"]
    assert option.transit_steps[2].instructions == ["经人行通道进入B口"]


def test_public_api_serializes_the_full_itinerary(monkeypatch):
    monkeypatch.delenv("AMAP_WEB_SERVICE_KEY", raising=False)
    payload = {"origin": {"name": "A", "point": {"lon": 113.3, "lat": 23.1}},
        "driver_destination": {"name": "B", "point": {"lon": 113.4, "lat": 23.2}},
        "passenger_destination": {"name": "C", "point": {"lon": 113.5, "lat": 23.3}},
        "preferences": {"modes": ["transit"]}}
    response = TestClient(app).post("/api/recommend", json=payload)
    assert response.status_code == 200
    option = response.json()["recommendations"][0]["options"][0]
    assert [step["kind"] for step in option["transit_steps"]] == ["walk", "subway", "walk", "subway", "walk"]
    assert option["transit_steps"][-1]["to_stop"] == "C"


def test_line_qualifiers_are_preserved_when_extracting_direction():
    item = itinerary()
    item["segments"][0]["bus"]["buslines"][0]["name"] = "M123路(区间快线)(甲站--乙站)"
    step = parse_itinerary(item, "020").transit_steps[1]
    assert step.line_name == "M123路(区间快线)" and step.direction == "乙站"


def test_final_walk_names_the_passengers_actual_destination():
    assert parse_itinerary(itinerary(), "020", "里城玺樾山").transit_steps[-1].to_stop == "里城玺樾山"

from fastapi.testclient import TestClient

from app.amap import AmapClient
from app.main import app
from app.models import Point


def test_driver_route_exposes_ordered_steps_without_duplicate_geometry():
    a, p, b = Point(lon=113.3, lat=23.1), Point(lon=113.31, lat=23.1), Point(lon=113.32, lat=23.1)
    path = {"distance": "2000", "cost": {"duration": "600"}, "steps": [
        {"road_name": "人民路", "instruction": "沿人民路行驶", "step_distance": "800",
         "navi": {"action": "右转", "assistant_action": "靠右"},
         "polyline": f"{a.csv()};{p.csv()}", "cost": {"duration": "200"}},
        {"road_name": "中山路", "distance": "1200", "polyline": f"{p.csv()};{b.csv()}",
         "cost": {"duration": "400"}}]}
    route = AmapClient.parse_driving({}, path, p)
    assert route.dropoff_duration_s == 200
    payload = route.model_dump(mode="json")
    assert "segments" not in payload
    assert "polyline" not in payload["driving_steps"][0]
    assert [s["road_name"] for s in payload["driving_steps"]] == ["人民路", "中山路"]
    assert payload["driving_steps"][0] == dict(road_name="人民路", instruction="沿人民路行驶",
        action="右转", assistant_action="靠右", distance_m=800, duration_s=200)
    assert sum(s["distance_m"] for s in payload["driving_steps"]) == payload["distance_m"]
    assert sum(s["duration_s"] for s in payload["driving_steps"]) == payload["duration_s"]


def test_missing_road_metadata_is_not_fabricated():
    route = AmapClient.parse_driving({}, {"distance": "100", "cost": {"duration": "20"}, "steps": [{}]})
    step = route.driving_steps[0]
    assert step.road_name == step.instruction == step.action == ""
    assert step.distance_m is None and step.duration_s is None


def test_demo_http_includes_baseline_and_driver_steps(monkeypatch):
    monkeypatch.delenv("AMAP_WEB_SERVICE_KEY", raising=False)
    with TestClient(app) as client:
        response = client.post("/api/recommend", json={
            "origin": {"name": "A", "point": {"lon": 113.3, "lat": 23.1}},
            "driver_destination": {"name": "B", "point": {"lon": 113.4, "lat": 23.1}},
            "passenger_destination": {"name": "C", "point": {"lon": 113.35, "lat": 23.2}},
            "preferences": {"modes": ["taxi"], "max_detour_minutes": 5}})
    assert response.status_code == 200
    payload = response.json()
    assert payload["baseline_route"]["driving_steps"]
    for rec in payload["recommendations"]:
        steps = rec["driver_route"]["driving_steps"]
        assert "前段" in steps[0]["road_name"] and "后段" in steps[1]["road_name"]
        assert sum(s["duration_s"] for s in steps) == rec["driver_route"]["duration_s"]
        assert sum(s["distance_m"] for s in steps) == rec["driver_route"]["distance_m"]

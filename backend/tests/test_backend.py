from datetime import datetime

from app.demo import create_demo
from app.geometry import haversine_m, sample_path
from app.models import Place, Point, Preferences, RecommendationRequest


def req(detour=5, mode=None):
    return RecommendationRequest(
        origin=Place(name="A", point=Point(lon=113.3245, lat=23.1065), citycode="020"),
        driver_destination=Place(name="B", point=Point(lon=113.3279, lat=23.1370), citycode="020"),
        passenger_destination=Place(name="C", point=Point(lon=113.269, lat=22.989), citycode="020"),
        preferences=Preferences(max_detour_minutes=detour, modes=mode or ["taxi", "transit"]))


def test_demo_constraints():
    result = create_demo(req(detour=3))
    assert result.mode == "demo"
    assert result.recommendations
    assert all(r.driver_detour_s <= 180 for r in result.recommendations)
    assert all(r.stop_status == "unverified" for r in result.recommendations)


def test_demo_zero_detour():
    result = create_demo(req(detour=0))
    assert result.recommendations == []  # Same 90-second stop allowance as live mode.


def test_demo_taxi_only():
    result = create_demo(req(mode=["taxi"]))
    assert all(len(r.options) == 1 and r.options[0].mode == "taxi"
               for r in result.recommendations)


def test_path_sampling():
    a = Point(lon=113, lat=23)
    b = Point(lon=114, lat=23)
    points = sample_path([a, b], count=3)
    assert len(points) == 3
    assert abs(points[1].lon - 113.5) < 1e-5
    assert haversine_m(a, b) > 100_000

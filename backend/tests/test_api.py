from fastapi.testclient import TestClient
import pytest
from app.main import app


def test_health_and_config(monkeypatch):
    monkeypatch.delenv('AMAP_WEB_SERVICE_KEY', raising=False)
    client = TestClient(app)
    assert client.get('/api/health').json()['status'] == 'ok'
    assert client.get('/api/config').json()['mode'] == 'demo'


def test_recommend_http(monkeypatch):
    monkeypatch.delenv('AMAP_WEB_SERVICE_KEY', raising=False)
    client = TestClient(app)
    payload = {
        'origin': {'name': 'A', 'point': {'lon': 113.3245, 'lat': 23.1065}, 'citycode': '020'},
        'driver_destination': {'name': 'B', 'point': {'lon': 113.3279, 'lat': 23.1370}, 'citycode': '020'},
        'passenger_destination': {'name': 'C', 'point': {'lon': 113.269, 'lat': 22.989}, 'citycode': '020'},
        'preferences': {'max_detour_minutes': 3, 'modes': ['taxi', 'transit'], 'priority': 'balanced'}
    }
    response = client.post('/api/recommend', json=payload)
    assert response.status_code == 200
    results = response.json()
    assert results['mode'] == 'demo'
    assert len(results['recommendations']) == 4
    assert len({(r['id'], r['suggested_mode']) for r in results['recommendations']}) == 4
    assert all(len(r['options']) == 1 for r in results['recommendations'])
    assert all(x['stop_status'] == 'unverified' for x in results['recommendations'])


def test_rejects_invalid_point(monkeypatch):
    monkeypatch.delenv('AMAP_WEB_SERVICE_KEY', raising=False)
    client = TestClient(app)
    response = client.post('/api/recommend', json={
        'origin': {'name': 'A', 'point': {'lon': 999, 'lat': 23}},
        'driver_destination': {'name': 'B', 'point': {'lon': 114, 'lat': 23}},
        'passenger_destination': {'name': 'C', 'point': {'lon': 115, 'lat': 23}}
    })
    assert response.status_code == 422


@pytest.mark.parametrize('path,valid_status', [('/api/recommend', 200), ('/api/evaluate-dropoff', 409)])
@pytest.mark.parametrize('minutes', [0, 120, 121])
def test_detour_limit_at_http_boundary(monkeypatch, path, valid_status, minutes):
    monkeypatch.delenv('AMAP_WEB_SERVICE_KEY', raising=False)
    payload = {
        'origin': {'name': 'A', 'point': {'lon': 113.3245, 'lat': 23.1065}},
        'driver_destination': {'name': 'B', 'point': {'lon': 113.3279, 'lat': 23.137}},
        'passenger_destination': {'name': 'C', 'point': {'lon': 113.269, 'lat': 22.989}},
        'dropoff': {'name': 'P', 'point': {'lon': 113.32, 'lat': 23.12}},
        'preferences': {'max_detour_minutes': minutes},
    }
    # Valid manual requests reach the live-map requirement in demo mode (409).
    status = valid_status if minutes <= 120 else 422
    assert TestClient(app).post(path, json=payload).status_code == status


def test_openapi_detour_default_and_maximum():
    field = TestClient(app).get('/openapi.json').json()['components']['schemas']['Preferences']['properties']['max_detour_minutes']
    assert field['default'] == field['maximum'] == 120


def test_openapi_transit_defaults():
    properties = TestClient(app).get('/openapi.json').json()['components']['schemas']['Preferences']['properties']
    assert properties['max_walking_m']['default'] == 3000
    assert properties['max_transfers']['default'] == 4

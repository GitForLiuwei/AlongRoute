import asyncio

import httpx
import pytest

from app.production import ProductionApp


def site(tmp_path):
    web = tmp_path / 'release' / 'web'
    web.mkdir(parents=True)
    (web / 'index.html').write_text('<html>AlongRoute</html>')
    (web / 'assets').mkdir()
    (web / 'assets' / 'app.js').write_text('console.log("AlongRoute")')
    (tmp_path / 'secret.env').write_text('not-public')
    return ProductionApp(web)


def test_static_and_api_are_served_on_same_origin(tmp_path, monkeypatch):
    monkeypatch.delenv('AMAP_WEB_SERVICE_KEY', raising=False)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=site(tmp_path)), base_url='http://test') as client:
            page = await client.get('/')
            assert page.status_code == 200 and 'AlongRoute' in page.text
            assert page.headers['x-alongroute-release'] == 'release'
            assert (await client.get('/assets/app.js')).status_code == 200
            assert (await client.get('/api/health')).json()['status'] == 'ok'
            assert (await client.get('/api/config')).json()['mode'] == 'demo'
            assert (await client.get('/api/unknown')).status_code == 404
            assert (await client.get('/.env')).status_code == 404
            assert (await client.get('/%2e%2e/secret.env')).status_code == 404
    asyncio.run(run())


def test_missing_build_fails_before_server_starts(tmp_path):
    with pytest.raises(RuntimeError, match='Frontend build missing'):
        ProductionApp(tmp_path)


def test_public_rate_limit_and_static_independence(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr('app.production.monotonic', lambda: clock[0])
    async def run():
        app = site(tmp_path)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            # Invalid bodies still consume planning quota, without map requests.
            for _ in range(6):
                assert (await client.post('/api/recommend', json={})).status_code == 422
            response = await client.post('/api/evaluate-dropoff', json={})
            assert response.status_code == 429 and response.headers['retry-after'] == '60'
            assert (await client.get('/')).status_code == 200
            clock[0] += 61
            assert (await client.post('/api/recommend', json={})).status_code == 422
    asyncio.run(run())


def test_map_proxy_keeps_backend_rules(tmp_path, monkeypatch):
    monkeypatch.setenv('AMAP_JS_SECURITY_CODE', 'test-secret')
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=site(tmp_path)), base_url='http://test') as client:
            assert (await client.get('/_AMapService/arbitrary')).status_code == 403
            assert (await client.post('/_AMapService/v3/foo')).status_code == 405
    asyncio.run(run())

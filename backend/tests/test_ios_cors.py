"""Verify the packaged WKWebView can plan and stream against FastAPI."""
import asyncio

import httpx

from app.main import app
from app.production import ProductionApp


def test_ios_origin_can_preflight_planning_and_read_diagnostics():
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.options("/api/recommend", headers={
                "Origin": "capacitor://localhost",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,x-search-id",
            })
            assert response.status_code == 200
            assert response.headers["access-control-allow-origin"] == "capacitor://localhost"
            # A validation error must still carry CORS headers for the app to show it.
            response = await client.post("/api/recommend", json={}, headers={"Origin": "capacitor://localhost"})
            assert response.status_code == 422
            assert response.headers["access-control-allow-origin"] == "capacitor://localhost"
            assert "X-Search-ID" in response.headers["access-control-expose-headers"]
            assert response.headers["x-search-id"]
            # EventSource and map proxy use simple GET requests from the same origin.
            for path in ("/api/health", "/api/search-replay/invalid/stream", "/_AMapService/invalid"):
                response = await client.get(path, headers={"Origin": "capacitor://localhost"})
                assert response.headers["access-control-allow-origin"] == "capacitor://localhost"
    asyncio.run(run())


def test_unlisted_web_origin_is_not_granted_cors():
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.options("/api/recommend", headers={
                "Origin": "https://unlisted.example",
                "Access-Control-Request-Method": "POST",
            })
            assert response.status_code == 400
            assert "access-control-allow-origin" not in response.headers
    asyncio.run(run())


def test_production_rate_limit_is_readable_by_ios(tmp_path):
    (tmp_path / "index.html").write_text("AlongRoute")
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ProductionApp(tmp_path)), base_url="http://test") as client:
            for _ in range(7):
                response = await client.post("/api/recommend", json={}, headers={"Origin": "capacitor://localhost"})
            assert response.status_code == 429
            assert response.headers["access-control-allow-origin"] == "capacitor://localhost"
            assert "Retry-After" in response.headers["access-control-expose-headers"]
    asyncio.run(run())

"""HTTP API: /api/config, /api/health, /api/places/suggest, /api/recommend."""
import asyncio
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from .amap import AmapClient, MapServiceError, calculate_live
from .demo import create_demo, demo_suggestions
from .models import RecommendationRequest

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
app = FastAPI(title="顺路下车 AlongRoute API", version="0.1.0")


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "AlongRoute"}


@app.get("/api/config")
def config():
    return {"mode": "live" if os.getenv("AMAP_WEB_SERVICE_KEY", "").strip() else "demo",
            "map_proxy_ready": bool(os.getenv("AMAP_JS_SECURITY_CODE", "").strip())}


@app.get("/api/places/suggest")
async def places_suggest(q: str = Query(min_length=1, max_length=80)):
    if not os.getenv("AMAP_WEB_SERVICE_KEY", "").strip():
        return {"mode": "demo", "items": demo_suggestions(q)}
    try:
        async with httpx.AsyncClient() as client:
            return {"mode": "live", "items": await AmapClient(
                os.environ["AMAP_WEB_SERVICE_KEY"], client).suggest(q)}
    except MapServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/recommend")
async def recommend(request: RecommendationRequest, http_request: Request):
    if not os.getenv("AMAP_WEB_SERVICE_KEY", "").strip():
        return create_demo(request)
    try:
        planning = asyncio.create_task(calculate_live(request))
        async def watch_disconnect():
            while not await http_request.is_disconnected():
                await asyncio.sleep(0.5)
        disconnect = asyncio.create_task(watch_disconnect())
        try:
            done, _ = await asyncio.wait({planning, disconnect}, timeout=110, return_when=asyncio.FIRST_COMPLETED)
            if planning in done:
                return planning.result()
            if disconnect in done:
                raise HTTPException(499, "客户端已取消计算")
            raise HTTPException(504, "规划超时，请稍后重试或缩小路线范围")
        finally:
            for task in (planning, disconnect):
                if not task.done():
                    task.cancel()
            await asyncio.gather(planning, disconnect, return_exceptions=True)
    except MapServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.api_route("/_AMapService/{path:path}", methods=["GET", "POST"])
async def js_api_proxy(path: str, request: Request):
    """Local-dev-only JS security proxy. Production requires IP limits / rate limits."""
    security = os.getenv("AMAP_JS_SECURITY_CODE", "").strip()
    if not security:
        raise HTTPException(503, "尚未配置高德 JS API 安全密钥")
    if request.method != "GET":
        raise HTTPException(405, "仅允许 GET")
    if not (path.startswith("v3/") or path.startswith("v4/") or path.startswith("v5/")):
        raise HTTPException(403, "非法代理路径")
    # Prevent uncontrolled general-purpose proxy; keys in URL remain server-side.
    upstream_base = "https://webapi.amap.com" if path.startswith("v4/map/styles") else "https://restapi.amap.com"
    params = [(k, v) for k, v in request.query_params.multi_items() if k != "jscode"]
    params.append(("jscode", security))
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            upstream = await client.get(f"{upstream_base}/{path}", params=params)
    except httpx.HTTPError as exc:
        raise HTTPException(502, "高德 JS 请求失败") from exc
    headers = {"cache-control": "private, max-age=30"}
    return Response(content=upstream.content, status_code=upstream.status_code,
                    media_type=upstream.headers.get("content-type"), headers=headers)

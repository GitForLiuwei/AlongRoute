"""HTTP API: /api/config, /api/health, /api/places/suggest, /api/recommend."""
import asyncio
import os
import re
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler

from .amap import AmapClient, MapServiceError, calculate_live
from .demo import create_demo, demo_suggestions
from .models import RecommendationRequest, ManualDropoffRequest, ManualDropoffResponse
from .manual import calculate_manual
from .search_log import start_trace, reset_trace, search_id, event, recommendation_summary, write_failed

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
app = FastAPI(title="顺路下车 AlongRoute API", version="0.1.0")


@app.middleware("http")
async def log_search(request: Request, call_next):
    kind = {"/api/recommend": "route", "/api/places/suggest": "place",
            "/api/evaluate-dropoff": "manual"}.get(request.url.path)
    if not kind:
        return await call_next(request)
    trace, token = start_trace(kind, trace_id=request.headers.get("X-Search-ID") if kind == "route" else None)
    trace.emit("search_started", method=request.method, path=request.url.path)
    try:
        response = await call_next(request)
        response.headers["X-Search-ID"] = trace.id
        status = ("cancelled" if response.status_code == 499 else "timeout" if response.status_code == 504
                  else "success" if response.status_code < 400 else "error")
        trace.emit("search_finished", status=status, http_status=response.status_code)
        if trace.write_failed:
            response.headers["X-Search-Log-Status"] = "unavailable"
        return response
    except asyncio.CancelledError:
        trace.emit("search_finished", status="cancelled")
        raise
    except Exception as exc:
        trace.emit("search_finished", status="error", error_type=type(exc).__name__)
        raise
    finally:
        reset_trace(token)


@app.exception_handler(RequestValidationError)
async def log_search_validation_error(request: Request, exc: RequestValidationError):
    event("validation_failed", errors=[dict(location=error["loc"], type=error["type"], message=error["msg"])
                                       for error in exc.errors()])
    return await request_validation_exception_handler(request, exc)


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "AlongRoute"}


@app.get("/api/config")
def config():
    return {"mode": "live" if os.getenv("AMAP_WEB_SERVICE_KEY", "").strip() else "demo",
            "map_proxy_ready": bool(os.getenv("AMAP_JS_SECURITY_CODE", "").strip())}


@app.get("/api/search-replay/{identifier}/stream")
async def search_replay_stream(identifier: str, request: Request):
    from .search_log import get_or_create_trace, TraceCapacityError
    from .search_replay import stream_replay

    if not re.fullmatch(r"[a-f0-9]{32}", identifier):
        raise HTTPException(422, "无效的日志标识")
    try:
        trace = get_or_create_trace(identifier)
    except TraceCapacityError as exc:
        raise HTTPException(503, str(exc)) from exc
    return StreamingResponse(stream_replay(trace, request), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.get("/api/search-replay/{identifier}")
def search_replay(identifier: str, response: Response):
    from .search_replay import read_replay
    response.headers["Cache-Control"] = "no-store"
    try:
        replay = read_replay(identifier)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(503, "搜索日志暂时无法读取") from exc
    if replay is None:
        raise HTTPException(404, "未找到该自动规划日志，可能已轮换清理；地点搜索和手动测算不支持候选回放")
    return replay


@app.get("/api/places/suggest")
async def places_suggest(q: str = Query(min_length=1, max_length=80)):
    event("place_query", query=q)
    if not os.getenv("AMAP_WEB_SERVICE_KEY", "").strip():
        items = demo_suggestions(q)
        event("place_results", mode="demo", count=len(items))
        return {"mode": "demo", "items": items}
    try:
        async with httpx.AsyncClient() as client:
            items = await AmapClient(os.environ["AMAP_WEB_SERVICE_KEY"], client).suggest(q)
            event("place_results", mode="live", count=len(items))
            return {"mode": "live", "items": items}
    except MapServiceError as exc:
        event("map_error", message=str(exc))
        raise HTTPException(status_code=502, detail=str(exc)) from exc


async def calculate_while_connected(calculate, http_request: Request):
    planning = asyncio.create_task(calculate)
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


@app.post("/api/evaluate-dropoff", response_model=ManualDropoffResponse)
async def evaluate_dropoff(request: ManualDropoffRequest, http_request: Request):
    event("manual_input", request=request.model_dump(mode="json"))
    if not os.getenv("AMAP_WEB_SERVICE_KEY", "").strip():
        raise HTTPException(409, "指定下车点测试需要配置真实地图服务，演示模式不生成虚构测算结果")
    try:
        result = await calculate_while_connected(calculate_manual(request), http_request)
    except MapServiceError as exc:
        event("map_error", message=str(exc))
        raise HTTPException(502, detail=str(exc)) from exc
    result.search_id = search_id()
    event("manual_result", driver_detour_s=result.driver_detour_s, fare_yuan=result.fare_yuan,
          passenger_total_s=result.passenger_total_s, combined_score=result.combined_score,
          within_detour_limit=result.within_detour_limit, within_taxi_fare_limit=result.within_taxi_fare_limit)
    if write_failed():
        result.warnings.append("本次搜索日志无法写入，请检查本地日志目录权限或磁盘空间。")
    return result


@app.post("/api/recommend")
async def recommend(request: RecommendationRequest, http_request: Request):
    event("route_input", request=request.model_dump(mode="json"),
          mode="live" if os.getenv("AMAP_WEB_SERVICE_KEY", "").strip() else "demo")
    def logged_result(result):
        result.search_id = search_id()
        event("route_result", mode=result.mode, examined_candidates=result.examined_candidates,
              search_complete=result.search_complete, warnings=result.warnings,
              baseline=dict(distance_m=result.baseline_route.distance_m, duration_s=result.baseline_route.duration_s),
              recommendations=[recommendation_summary(rec) for rec in result.recommendations])
        if write_failed():
            result.warnings.append("本次搜索日志无法写入，请检查本地日志目录权限或磁盘空间。")
        return result
    if not os.getenv("AMAP_WEB_SERVICE_KEY", "").strip():
        return logged_result(create_demo(request))
    try:
        return logged_result(await calculate_while_connected(calculate_live(request), http_request))
    except MapServiceError as exc:
        event("map_error", message=str(exc))
        raise HTTPException(status_code=502, detail=str(exc)) from exc


_proxy_http_client: httpx.AsyncClient | None = None


def _get_proxy_http_client() -> httpx.AsyncClient:
    """Return a shared, pooled AsyncClient for AMap JS API proxy requests."""
    global _proxy_http_client
    if _proxy_http_client is None or _proxy_http_client.is_closed:
        _proxy_http_client = httpx.AsyncClient(
            timeout=20.0,
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=64),
        )
    return _proxy_http_client


@app.on_event("shutdown")
async def _close_proxy_http_client() -> None:
    global _proxy_http_client
    if _proxy_http_client is not None and not _proxy_http_client.is_closed:
        await _proxy_http_client.aclose()
        _proxy_http_client = None


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
        client = _get_proxy_http_client()
        upstream = await client.get(f"{upstream_base}/{path}", params=params)
    except httpx.HTTPError as exc:
        raise HTTPException(502, "高德 JS 请求失败") from exc
    headers = {"cache-control": "private, max-age=30"}
    return Response(content=upstream.content, status_code=upstream.status_code,
                    media_type=upstream.headers.get("content-type"), headers=headers)

"""Serve the built frontend and existing API from one production listener."""
import os
from collections import OrderedDict, deque
from pathlib import Path
from time import monotonic

from starlette.responses import JSONResponse
from starlette.middleware.exceptions import ExceptionMiddleware
from starlette.staticfiles import StaticFiles
from starlette.types import Receive, Scope, Send

from .main import app as api


class ProductionApp:
    def __init__(self, static_dir: Path):
        if not (static_dir / "index.html").is_file():
            raise RuntimeError("Frontend build missing; run scripts/deploy-linux.sh first")
        self.static = ExceptionMiddleware(StaticFiles(directory=static_dir, html=True))
        self.release = static_dir.parent.name
        self.requests = OrderedDict()
        self.global_requests = deque()

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            return await api(scope, receive, send)
        original_send = send

        async def send(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []),
                           (b"x-alongroute-release", self.release.encode("ascii", errors="replace"))]}
            await original_send(message)
        path = scope["path"]
        is_api = path == "/api" or path.startswith("/api/")
        is_map = path == "/_AMapService" or path.startswith("/_AMapService/")
        if is_api or is_map:
            now = monotonic()
            client = (scope.get("client") or ("unknown", 0))[0]
            planning = path in {"/api/recommend", "/api/evaluate-dropoff"}
            bucket = "planning" if planning else "map" if is_map else "api"
            key = (client, bucket)
            if key not in self.requests:
                if len(self.requests) >= 4096:
                    self.requests.popitem(last=False)
                self.requests[key] = deque()
            self.requests.move_to_end(key)
            recent = self.requests[key]
            for queue in (recent, self.global_requests):
                while queue and queue[0] <= now - 60:
                    queue.popleft()
            limit = 6 if planning else 180 if is_map else 60
            if len(recent) >= limit or len(self.global_requests) >= 600:
                response = JSONResponse({"detail": "请求过于频繁，请稍后再试"}, status_code=429,
                                        headers={"Retry-After": "60"})
                return await response(scope, receive, send)
            recent.append(now)
            self.global_requests.append(now)
            return await api(scope, receive, send)
        if path in {"/docs", "/docs/oauth2-redirect", "/openapi.json", "/redoc"}:
            return await api(scope, receive, send)
        # Explicit frontend routes support direct visits and browser refreshes.
        # Unknown paths still return 404, including missing assets and API routes.
        if path in {"/user", "/user/", "/debug", "/debug/"}:
            scope = {**scope, "path": "/index.html", "raw_path": b"/index.html"}
        return await self.static(scope, receive, send)


def create_app():
    default = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    return ProductionApp(Path(os.environ.get("ALONGROUTE_STATIC_DIR", str(default))))

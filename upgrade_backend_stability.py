#!/usr/bin/env python3
"""AlongRoute 后端稳定性、连接池与 SQLite WAL 性能优化一键脚本 (针对 commit e2f2505).

用法:
    1. 预览修改内容 (Dry Run，不修改磁盘文件):
       python3 upgrade_backend_stability.py --dry-run

    2. 在 AlongRoute 仓库根目录下直接应用修改并生成 .bak 备份:
       python3 upgrade_backend_stability.py --backup

涵盖的 4 项核心改进:
    [1] backend/app/search_replay.py & backend/app/search_log.py:
        - stream_replay 超时对齐为 115.0s（略大于主规划 110s 超时）；
        - 增加孤儿流快速退出机制：若客户端连接 SSE 后满 6 秒 trace.sequence 仍为 0
          （例如 POST /api/recommend 因参数校验 422 或限流 429 未启动），立即关闭流而不空挂 115 秒；
        - SearchTrace.subscribe 使用有界队列 asyncio.Queue(maxsize=512)，满队列时安全丢弃防内存泄漏。
    [2] backend/app/main.py:
        - 为 js_api_proxy (/_AMapService) 引入全局共享的 httpx.AsyncClient 连接池
          (max_keepalive_connections=20, max_connections=64)，并在应用退出时自动优雅关闭，
          消除全屏高德底图下的高频 TCP/TLS 重复握手开销与端口耗尽隐患。
    [3] backend/app/map_cache.py:
        - 在 _database() 中开启 SQLite WAL 并发日志模式 (PRAGMA journal_mode=WAL;
          PRAGMA synchronous=NORMAL; PRAGMA busy_timeout=5000;)，并仅在首次建表时执行 DDL；
        - 在 get() 与 put() 中将原本「每次读写都执行 DELETE 过期行 + 每次写入都全表扫描 SUM(LENGTH(payload))」
          优化为每 32 次写入触发一次批量淘汰 (_write_count % 32 == 0)，大幅降低磁盘 I/O 锁竞争。
    [4] backend/app/production.py:
        - 支持反向代理 (Nginx / Caddy) 环境下的真实客户端 IP 识别：当对端为本地回环地址
          (127.0.0.1 / ::1) 时，自动解析 X-Forwarded-For 或 X-Real-IP 请求头，防止全站用户共享
          127.0.0.1 限流桶导致误报 429；
        - 为 /api/search-replay/.../stream 的 SSE 重连请求单独设置 stream 限流桶。
"""

from __future__ import annotations

import argparse
import ast
import difflib
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class PatchResult:
    file_rel: str
    applied: list[str]
    skipped: list[str]
    changed: bool


# ============================================================================
# 1. backend/app/search_log.py & backend/app/search_replay.py
# ============================================================================

def patch_search_log(content: str) -> tuple[str, list[str], list[str]]:
    applied, skipped = [], []

    if "asyncio.Queue(maxsize=512)" in content:
        skipped.append("search_log.subscribe 有界队列 maxsize=512 已存在")
    elif "queue = asyncio.Queue()" in content:
        content = content.replace(
            "queue = asyncio.Queue()",
            "queue = asyncio.Queue(maxsize=512)",
            1,
        )
        applied.append("将 SearchTrace.subscribe 队列设为有界队列 asyncio.Queue(maxsize=512)")
    elif "queue: asyncio.Queue = asyncio.Queue()" in content:
        content = content.replace(
            "queue: asyncio.Queue = asyncio.Queue()",
            "queue: asyncio.Queue = asyncio.Queue(maxsize=512)",
            1,
        )
        applied.append("将 SearchTrace.subscribe 队列设为有界队列 asyncio.Queue(maxsize=512)")
    else:
        skipped.append("未找到无界 asyncio.Queue() 初始化")

    return content, applied, skipped


STREAM_REPLAY_NEW = '''async def stream_replay(trace, request=None):
    """Send the snapshot and subsequent events, with 115s timeout alignment and orphan fast-exit."""
    loop = asyncio.get_running_loop()
    snapshot, queue = trace.subscribe(loop)
    snapshot = deque(snapshot)
    started_at = loop.time()
    deadline = started_at + 115.0  # Aligned with planning's 110s server timeout (+5s grace).
    orphan_deadline = started_at + 6.0  # Fast-exit if POST /api/recommend never starts this trace.
    seen_any_event = bool(snapshot) or getattr(trace, "sequence", 0) > 0
    try:
        # Flush headers immediately, even if the POST has not arrived yet.
        yield ': connected\\n\\n'
        while True:
            if snapshot:
                record = snapshot.popleft()
                seen_any_event = True
            else:
                now = loop.time()
                if now >= deadline or (request is not None and await request.is_disconnected()):
                    return
                if not seen_any_event and getattr(trace, "sequence", 0) == 0 and now >= orphan_deadline:
                    yield 'event: done\\ndata: {"finished": false, "reason": "orphan_timeout"}\\n\\n'
                    return
                try:
                    record = await asyncio.wait_for(queue.get(), timeout=1.0)
                    seen_any_event = True
                except asyncio.TimeoutError:
                    yield ': keep-alive\\n\\n'
                    continue
            item = format_replay_event(record)
            if item is not None:
                yield f'data: {json.dumps(item, ensure_ascii=False, allow_nan=False)}\\n\\n'
            if record.get('event') == 'search_finished':
                yield 'event: done\\ndata: {"finished": true}\\n\\n'
                return
    finally:
        trace.unsubscribe(queue)'''


def patch_search_replay(content: str) -> tuple[str, list[str], list[str]]:
    applied, skipped = [], []

    if "orphan_deadline" in content:
        skipped.append("stream_replay 115s 超时对齐与 6s 空流快速退出已存在")
        return content, applied, skipped

    pattern = re.compile(
        r"async def stream_replay\(trace,\s*request=None\):.*?"
        r"finally:\n[ \t]+trace\.unsubscribe\(queue\)",
        re.DOTALL,
    )
    if pattern.search(content):
        content = pattern.sub(lambda _: STREAM_REPLAY_NEW, content, count=1)
        applied.append("升级 stream_replay：超时对齐 115.0s，并增加 6.0s 未启动空流快速退出保护")
    else:
        skipped.append("未匹配到 stream_replay 函数定义")

    return content, applied, skipped


# ============================================================================
# 2. backend/app/main.py: js_api_proxy 共享 httpx.AsyncClient 连接池
# ============================================================================

MAIN_PROXY_NEW = '''_proxy_http_client: httpx.AsyncClient | None = None


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
                    media_type=upstream.headers.get("content-type"), headers=headers)'''


def patch_main(content: str) -> tuple[str, list[str], list[str]]:
    applied, skipped = [], []

    if "_get_proxy_http_client" in content:
        skipped.append("main.py js_api_proxy 共享 httpx 连接池已存在")
        return content, applied, skipped

    pattern = re.compile(
        r'@app\.api_route\("/_AMapService/\{path:path\}",\s*methods=\["GET",\s*"POST"\]\)\n'
        r"async def js_api_proxy\(path:\s*str,\s*request:\s*Request\):.*?"
        r'media_type=upstream\.headers\.get\("content-type"\),\s*headers=headers\)',
        re.DOTALL,
    )
    if pattern.search(content):
        content = pattern.sub(lambda _: MAIN_PROXY_NEW, content, count=1)
        applied.append("重构 js_api_proxy：复用全局共享 httpx.AsyncClient 连接池 (keepalive=20, max=64)")
    else:
        skipped.append("未匹配到 js_api_proxy 函数定义")

    return content, applied, skipped


# ============================================================================
# 3. backend/app/map_cache.py: 开启 SQLite WAL 模式与每 32 次写入降频 Prune
# ============================================================================

MAP_CACHE_DB_INIT_NEW = '''        self._entries = OrderedDict()
        self._bytes = 0
        self._lock = Lock()
        self._storage_warning = False
        self._db_initialized = False
        self._write_count = 0

    @contextmanager
    def _database(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path, timeout=5.0)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA busy_timeout=5000")
            if not self._db_initialized:
                with connection:
                    connection.execute("""CREATE TABLE IF NOT EXISTS responses (
                        cache_key BLOB PRIMARY KEY,
                        expires_at REAL NOT NULL,
                        accessed_at REAL NOT NULL,
                        payload BLOB NOT NULL
                    )""")
                    connection.execute(
                        "CREATE INDEX IF NOT EXISTS idx_responses_accessed_at ON responses (accessed_at)"
                    )
                    connection.execute(
                        "CREATE INDEX IF NOT EXISTS idx_responses_expires_at ON responses (expires_at)"
                    )
                self._db_initialized = True
            yield connection
        finally:
            connection.close()'''


MAP_CACHE_GET_PUT_NEW = '''    def get(self, key):
        with self._lock:
            now = time()
            try:
                with self._database() as database:
                    entry = database.execute(
                        "SELECT expires_at, payload FROM responses WHERE cache_key = ?", (key,)
                    ).fetchone()
                    if entry is not None:
                        if entry[0] <= now:
                            with database:
                                database.execute("DELETE FROM responses WHERE cache_key = ?", (key,))
                            entry = None
                        else:
                            with database:
                                database.execute("UPDATE responses SET accessed_at = ? WHERE cache_key = ?", (now, key))
                self._storage_warning = False
            except (OSError, sqlite3.Error):
                self._storage_failed()
                entry = self._entries.get(key)
            if entry is None or entry[0] <= now:
                if key in self._entries:
                    self._remove(key)
                return None
            expires, payload = entry
            self._remember(key, expires, payload)
            # Deserialize a fresh value so callers cannot modify cached data.
            try:
                return json.loads(payload)
            except (ValueError, UnicodeError):
                return None

    def put(self, key, path, data):
        ttl = TTL_SECONDS.get(path, 0)
        if ttl <= 0 or not isinstance(data, dict) or str(data.get("status")) != "1":
            return
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        if len(payload) > self.max_bytes:
            return
        with self._lock:
            now = time()
            expires = now + ttl
            self._remember(key, expires, payload)
            self._write_count += 1
            should_prune = (self._write_count == 1) or (self._write_count % 32 == 0)
            try:
                with self._database() as database:
                    with database:
                        database.execute("""INSERT OR REPLACE INTO responses
                            (cache_key, expires_at, accessed_at, payload) VALUES (?, ?, ?, ?)""",
                            (key, expires, now, payload))
                        if should_prune:
                            database.execute("DELETE FROM responses WHERE expires_at <= ?", (now,))
                            count, size = database.execute(
                                "SELECT COUNT(*), COALESCE(SUM(LENGTH(payload)), 0) FROM responses"
                            ).fetchone()
                            if count > self.max_entries or size > self.max_bytes:
                                for old_key, old_size in database.execute(
                                    "SELECT cache_key, LENGTH(payload) FROM responses ORDER BY accessed_at, cache_key"
                                ).fetchall():
                                    if count <= self.max_entries and size <= self.max_bytes:
                                        break
                                    database.execute("DELETE FROM responses WHERE cache_key = ?", (old_key,))
                                    count -= 1
                                    size -= old_size
                self._storage_warning = False
            except (OSError, sqlite3.Error):
                self._storage_failed()'''


def patch_map_cache(content: str) -> tuple[str, list[str], list[str]]:
    applied, skipped = [], []

    if "PRAGMA journal_mode=WAL" in content:
        skipped.append("map_cache SQLite WAL 模式与降频 prune 已存在")
        return content, applied, skipped

    init_db_pat = re.compile(
        r"        self\._entries = OrderedDict\(\)\n"
        r"        self\._bytes = 0\n"
        r"        self\._lock = Lock\(\)\n"
        r"        self\._storage_warning = False\n\n"
        r"    @contextmanager\n"
        r"    def _database\(self\):.*?"
        r"        finally:\n"
        r"            connection\.close\(\)",
        re.DOTALL,
    )
    if init_db_pat.search(content):
        content = init_db_pat.sub(lambda _: MAP_CACHE_DB_INIT_NEW, content, count=1)
        applied.append("在 MapResponseCache._database 中开启 WAL 模式、busy_timeout=5000 及索引初始化")
    else:
        skipped.append("未匹配到 MapResponseCache._database 块")

    get_put_pat = re.compile(
        r"    def get\(self, key\):.*?"
        r"            except \(OSError, sqlite3\.Error\):\n"
        r"                self\._storage_failed\(\)",
        re.DOTALL,
    )
    if get_put_pat.search(content):
        content = get_put_pat.sub(lambda _: MAP_CACHE_GET_PUT_NEW, content, count=1)
        applied.append("优化 MapResponseCache.get/put：移除每次 get 的全表过期清理，并将 put 的容量淘汰降频至每 32 次写入触发")
    else:
        skipped.append("未匹配到 MapResponseCache.get/put 块")

    return content, applied, skipped


# ============================================================================
# 4. backend/app/production.py: X-Forwarded-For 真实 IP 识别与 SSE 流式桶隔离
# ============================================================================

PRODUCTION_RATE_LIMIT_NEW = '''        if is_api or is_map:
            now = monotonic()
            peer_ip = (scope.get("client") or ("unknown", 0))[0]
            client = peer_ip
            if peer_ip in {"127.0.0.1", "::1", "localhost"}:
                raw_headers = dict(scope.get("headers") or [])
                xff = raw_headers.get(b"x-forwarded-for", b"").decode("latin1").strip()
                xri = raw_headers.get(b"x-real-ip", b"").decode("latin1").strip()
                if xff:
                    client = xff.split(",")[0].strip() or peer_ip
                elif xri:
                    client = xri
            planning = path in {"/api/recommend", "/api/evaluate-dropoff"}
            is_stream = path.startswith("/api/search-replay/") and path.endswith("/stream")
            bucket = "planning" if planning else "map" if is_map else "stream" if is_stream else "api"
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
            limit = 6 if planning else 180 if is_map else 120 if is_stream else 60'''


def patch_production(content: str) -> tuple[str, list[str], list[str]]:
    applied, skipped = [], []

    if "x-forwarded-for" in content:
        skipped.append("production.py X-Forwarded-For 真实客户端 IP 解析已存在")
        return content, applied, skipped

    pattern = re.compile(
        r"        if is_api or is_map:\n"
        r"            now = monotonic\(\)\n"
        r'            client = \(scope\.get\("client"\) or \("unknown", 0\)\)\[0\]\n'
        r'            planning = path in \{"/api/recommend", "/api/evaluate-dropoff"\}\n'
        r'            bucket = "planning" if planning else "map" if is_map else "api"\n'
        r"            key = \(client, bucket\)\n"
        r"            if key not in self\.requests:\n"
        r"                if len\(self\.requests\) >= 4096:\n"
        r"                    self\.requests\.popitem\(last=False\)\n"
        r"                self\.requests\[key\] = deque\(\)\n"
        r"            self\.requests\.move_to_end\(key\)\n"
        r"            recent = self\.requests\[key\]\n"
        r"            for queue in \(recent, self\.global_requests\):\n"
        r"                while queue and queue\[0\] <= now - 60:\n"
        r"                    queue\.popleft\(\)\n"
        r"            limit = 6 if planning else 180 if is_map else 60"
    )
    if pattern.search(content):
        content = pattern.sub(lambda _: PRODUCTION_RATE_LIMIT_NEW, content, count=1)
        applied.append("升级 ProductionApp 限流器：支持本地反代解析 X-Forwarded-For / X-Real-IP 真实 IP，并独立划分 SSE stream 桶")
    else:
        skipped.append("未匹配到 ProductionApp 限流块")

    return content, applied, skipped


# ============================================================================
# 主执行器
# ============================================================================

PATCH_PIPELINE = [
    ("backend/app/search_log.py", patch_search_log),
    ("backend/app/search_replay.py", patch_search_replay),
    ("backend/app/main.py", patch_main),
    ("backend/app/map_cache.py", patch_map_cache),
    ("backend/app/production.py", patch_production),
]


def apply_all(repo_dir: Path, dry_run: bool = False, backup: bool = False) -> list[PatchResult]:
    results: list[PatchResult] = []
    for rel_path, patcher in PATCH_PIPELINE:
        fpath = repo_dir / rel_path
        if not fpath.exists():
            results.append(PatchResult(rel_path, [], [f"文件不存在: {fpath}"], False))
            continue

        original = fpath.read_text(encoding="utf-8")
        updated, applied, skipped = patcher(original)
        changed = updated != original

        if changed:
            try:
                ast.parse(updated, filename=str(fpath))
            except SyntaxError as exc:
                raise RuntimeError(f"Python 语法校验失败 ({rel_path}): {exc}") from exc

            if dry_run:
                diff = difflib.unified_diff(
                    original.splitlines(keepends=True),
                    updated.splitlines(keepends=True),
                    fromfile=f"a/{rel_path}",
                    tofile=f"b/{rel_path}",
                )
                sys.stdout.writelines(diff)
            else:
                if backup:
                    shutil.copy2(fpath, fpath.with_suffix(fpath.suffix + ".bak"))
                fpath.write_text(updated, encoding="utf-8")

        results.append(PatchResult(rel_path, applied, skipped, changed))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="AlongRoute 后端稳定性、连接池与 SQLite WAL 优化一键脚本")
    parser.add_argument(
        "--repo-dir",
        type=Path,
        default=Path.cwd(),
        help="AlongRoute 仓库根目录路径 (默认: 当前目录)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅打印 Unified Diff 预览，不修改磁盘文件",
    )
    parser.add_argument(
        "--backup",
        action="store_true",
        help="修改前为每个受影响的文件创建 .bak 备份",
    )
    args = parser.parse_args()

    repo_dir = args.repo_dir.resolve()
    if not (repo_dir / "backend" / "app").exists():
        print(
            f"[提示] 在 {repo_dir} 下未检测到 backend/app 目录，请通过 --repo-dir 指定 AlongRoute 仓库根目录。",
            file=sys.stderr,
        )
        return 1

    results = apply_all(repo_dir, dry_run=args.dry_run, backup=args.backup)

    mode_label = "DRY-RUN 预览" if args.dry_run else "已写入磁盘"
    print(f"\n=== AlongRoute 后端稳定性与性能升级摘要 ({mode_label}) ===")
    for res in results:
        status = "UPDATED" if res.changed else "UNCHANGED"
        print(f"\n[{status}] {res.file_rel}")
        for item in res.applied:
            print(f"  + {item}")
        for item in res.skipped:
            print(f"  - (跳过) {item}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""Local, request-scoped planning diagnostics. Never log map URLs or credentials."""
import asyncio
from collections import OrderedDict
from contextvars import ContextVar
from datetime import datetime
from functools import lru_cache
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import re
import threading
from time import perf_counter
from uuid import uuid4
from zoneinfo import ZoneInfo


LOG_DIR = Path(__file__).resolve().parents[2] / ".runtime" / "search-logs"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
_current = ContextVar("search_trace", default=None)
_ACTIVE_TRACES_LOCK = threading.Lock()
_ACTIVE_TRACES = OrderedDict()
_MAX_ACTIVE_TRACES = 64
_sensitive = {"key", "apikey", "jscode", "securitycode", "authorization", "token", "password",
              "amapwebservicekey", "amapjssecuritycode", "viteamapjskey"}


class _SearchFileHandler(RotatingFileHandler):
    def handleError(self, record):
        # Report failure to the caller without printing a complete log record.
        raise OSError("Unable to write search log")


def _redact(value):
    if isinstance(value, dict):
        return {k: "[REDACTED]" if re.sub(r"[^a-z]", "", str(k).lower()) in _sensitive else _redact(v)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, str):
        for name in ("AMAP_WEB_SERVICE_KEY", "AMAP_JS_SECURITY_CODE", "VITE_AMAP_JS_KEY"):
            secret = os.getenv(name, "").strip()
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return re.sub(r"(?i)((?:key|jscode|token|authorization)=)[^\s&]+", r"\1[REDACTED]", value)
    return value


@lru_cache(maxsize=7)
def _logger(path: str):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    logger = logging.Logger(f"search:{path}")
    handler = _SearchFileHandler(path, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    return logger


class SearchTrace:
    def __init__(self, kind, trace_id=None):
        self.id = trace_id or uuid4().hex
        self.kind = kind
        self.started = perf_counter()
        self.sequence = 0
        self.write_failed = False
        self.records = []
        self._lock = threading.Lock()
        self._subscribers = []
        self.claimed = False
        self.finished = False

    def emit(self, event, **fields):
        now = datetime.now(LOCAL_TZ)
        # Snapshot/subscription and publishing share a lock: no event can fall between them.
        with self._lock:
            self.sequence += 1
            record = _redact({**fields, "time": now.isoformat(timespec="milliseconds"), "search_id": self.id,
                             "kind": self.kind, "sequence": self.sequence,
                             "elapsed_ms": round((perf_counter() - self.started) * 1000), "event": event})
            if self.kind == "route":
                self.records.append(record)
            self.finished = event == "search_finished" or self.finished
            for loop, queue in self._subscribers:
                try:
                    loop.call_soon_threadsafe(queue.put_nowait, record)
                except RuntimeError:
                    pass  # The disconnected subscriber's loop has already closed.
        try:
            # Every event is flushed by the handler, including starts and cancellations.
            _logger(str(LOG_DIR / f"{now:%Y-%m-%d}.jsonl")).info(
                json.dumps(record, ensure_ascii=False, allow_nan=False))
        except (OSError, ValueError, TypeError):
            if not self.write_failed:
                logging.getLogger(__name__).warning("Search log write failed (search_id=%s)", self.id)
            self.write_failed = True

    def snapshot(self):
        with self._lock:
            return list(self.records)

    def subscribe(self, loop):
        queue = asyncio.Queue(maxsize=512)
        with self._lock:
            snapshot = list(self.records)
            self._subscribers.append((loop, queue))
        return snapshot, queue

    def unsubscribe(self, queue):
        with self._lock:
            self._subscribers = [(loop, q) for loop, q in self._subscribers if q is not queue]


def _valid_trace_id(identifier):
    if isinstance(identifier, str):
        identifier = identifier.strip().lower()
        if re.fullmatch(r"[a-f0-9]{32}", identifier):
            return identifier
    return None


class TraceCapacityError(RuntimeError):
    pass


def _registered_trace(identifier=None):
    """Called with the registry lock held; never evict an in-flight search."""
    if identifier in _ACTIVE_TRACES:
        _ACTIVE_TRACES.move_to_end(identifier)
        return _ACTIVE_TRACES[identifier]
    if len(_ACTIVE_TRACES) >= _MAX_ACTIVE_TRACES:
        for key, trace in list(_ACTIVE_TRACES.items()):
            with trace._lock:
                # Keep fresh SSE placeholders until the matching POST can claim them,
                # including the brief gap before StreamingResponse starts iteration.
                evictable = not trace._subscribers and (trace.finished or
                            (not trace.claimed and perf_counter() - trace.started > 120))
            if evictable:
                del _ACTIVE_TRACES[key]
                break
        else:
            raise TraceCapacityError("实时搜索连接已满，请稍后重试")
    trace = SearchTrace("route", identifier)
    _ACTIVE_TRACES[trace.id] = trace
    return trace


def get_or_create_trace(trace_id=None, kind="route"):
    if kind != "route":
        return SearchTrace(kind)
    with _ACTIVE_TRACES_LOCK:
        return _registered_trace(_valid_trace_id(trace_id))


def get_active_trace(trace_id):
    with _ACTIVE_TRACES_LOCK:
        return _ACTIVE_TRACES.get(_valid_trace_id(trace_id))


def start_trace(kind, trace_id=None):
    if kind == "route":
        with _ACTIVE_TRACES_LOCK:
            try:
                trace = _registered_trace(_valid_trace_id(trace_id))
                # Only a pre-registered SSE placeholder may be claimed. Repeated IDs
                # must not merge separate POST requests or resurrect completed searches.
                if trace.claimed:
                    trace = _registered_trace()
                trace.claimed = True
                trace.started = perf_counter()
            except TraceCapacityError:
                # Optional diagnostics must not prevent the recommendation itself.
                trace = SearchTrace(kind)
    else:
        trace = SearchTrace(kind)
    return trace, _current.set(trace)


def reset_trace(token):
    _current.reset(token)


def search_id():
    trace = _current.get()
    return trace.id if trace else None


def write_failed():
    trace = _current.get()
    return bool(trace and trace.write_failed)


def event(name, **fields):
    trace = _current.get()
    if trace:
        trace.emit(name, **fields)


def recommendation_summary(rec):
    return dict(id=rec.id, point=rec.point.model_dump(mode="json"),
                driver_detour_s=rec.driver_detour_s, shared_ride_s=rec.shared_ride_s,
                stop_duration_s=rec.stop_duration_s, driver_distance_m=rec.driver_route.distance_m,
                driver_duration_s=rec.driver_route.duration_s, suggested_mode=rec.suggested_mode,
                label=rec.recommendation_label, reason=rec.reason,
                options=[dict(mode=o.mode, fare_yuan=o.fare_yuan, duration_s=o.duration_s,
                              total_duration_s=o.total_duration_s, boarding_walk_m=o.boarding_walk_m,
                              walking_m=o.walking_m, transfers=o.transfers) for o in rec.options])

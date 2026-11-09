"""Local, request-scoped planning diagnostics. Never log map URLs or credentials."""
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
from time import perf_counter
from uuid import uuid4
from zoneinfo import ZoneInfo


LOG_DIR = Path(__file__).resolve().parents[2] / ".runtime" / "search-logs"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
_current = ContextVar("search_trace", default=None)
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
    def __init__(self, kind):
        self.id = uuid4().hex
        self.kind = kind
        self.started = perf_counter()
        self.sequence = 0
        self.write_failed = False

    def emit(self, event, **fields):
        self.sequence += 1
        now = datetime.now(LOCAL_TZ)
        # Diagnostic payloads cannot overwrite request identity or crash on a reserved field.
        record = {**fields, "time": now.isoformat(timespec="milliseconds"), "search_id": self.id,
                  "kind": self.kind, "sequence": self.sequence,
                  "elapsed_ms": round((perf_counter() - self.started) * 1000), "event": event}
        try:
            # Every event is flushed by the handler, including starts and cancellations.
            _logger(str(LOG_DIR / f"{now:%Y-%m-%d}.jsonl")).info(
                json.dumps(_redact(record), ensure_ascii=False, allow_nan=False))
        except (OSError, ValueError, TypeError):
            if not self.write_failed:
                logging.getLogger(__name__).warning("Search log write failed (search_id=%s)", self.id)
            self.write_failed = True


def start_trace(kind):
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

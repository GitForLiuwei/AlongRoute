"""Bounded SQLite TTL cache, retained across backend and machine restarts."""
from collections import OrderedDict
from contextlib import contextmanager
from hashlib import sha256
import json
import logging
from pathlib import Path
import sqlite3
from threading import Lock
from time import time


CACHE_TTL_SECONDS = 24 * 60 * 60
CACHE_PATH = Path(__file__).resolve().parents[2] / ".runtime" / "api-cache.sqlite3"
TTL_SECONDS = {
    "/v5/place/text": CACHE_TTL_SECONDS,
    "/v5/place/around": CACHE_TTL_SECONDS,
    "/v5/direction/walking": CACHE_TTL_SECONDS,
    "/v5/direction/driving": CACHE_TTL_SECONDS,
    "/v5/direction/transit/integrated": CACHE_TTL_SECONDS,
}


class MapResponseCache:
    def __init__(self, max_entries=512, max_bytes=32 * 1024 * 1024, db_path=CACHE_PATH):
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.db_path = Path(db_path)
        # Memory fallback keeps planning available if the disk is temporarily unavailable.
        self._entries = OrderedDict()
        self._bytes = 0
        self._lock = Lock()
        self._storage_warning = False

    @contextmanager
    def _database(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path, timeout=1)
        try:
            with connection:
                connection.execute("""CREATE TABLE IF NOT EXISTS responses (
                    cache_key BLOB PRIMARY KEY,
                    expires_at REAL NOT NULL,
                    accessed_at REAL NOT NULL,
                    payload BLOB NOT NULL
                )""")
                yield connection
        finally:
            connection.close()

    def _storage_failed(self):
        if not self._storage_warning:
            logging.getLogger(__name__).warning("地图API磁盘缓存暂不可用，当前使用内存缓存")
            self._storage_warning = True

    def _remember(self, key, expires, payload):
        now = time()
        for expired in [k for k, (expiry, _) in self._entries.items() if expiry <= now]:
            self._remove(expired)
        if key in self._entries:
            self._remove(key)
        self._entries[key] = (expires, payload)
        self._bytes += len(payload)
        while len(self._entries) > self.max_entries or self._bytes > self.max_bytes:
            self._remove(next(iter(self._entries)))

    @staticmethod
    def key(credential, path, params):
        # Credentials isolate entries without retaining keys in cache metadata.
        payload = json.dumps([credential, path, params], sort_keys=True, ensure_ascii=False)
        return sha256(payload.encode("utf-8")).digest()

    def _remove(self, key):
        _, payload = self._entries.pop(key)
        self._bytes -= len(payload)

    def get(self, key):
        with self._lock:
            now = time()
            try:
                with self._database() as database:
                    database.execute("DELETE FROM responses WHERE expires_at <= ?", (now,))
                    entry = database.execute(
                        "SELECT expires_at, payload FROM responses WHERE cache_key = ?", (key,)
                    ).fetchone()
                    if entry is not None:
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
            try:
                with self._database() as database:
                    database.execute("DELETE FROM responses WHERE expires_at <= ?", (now,))
                    database.execute("""INSERT OR REPLACE INTO responses
                        (cache_key, expires_at, accessed_at, payload) VALUES (?, ?, ?, ?)""",
                        (key, expires, now, payload))
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
                self._storage_failed()


response_cache = MapResponseCache()

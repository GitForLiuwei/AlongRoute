import pytest

from app.map_cache import MapResponseCache
from app.hub_store import HubStore


@pytest.fixture(autouse=True)
def isolated_map_cache(monkeypatch, tmp_path):
    # Offline mocks must not depend on the developer's proxy or optional SOCKS extras.
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    # Mocked map data must never enter the running application's persistent cache.
    monkeypatch.setattr("app.amap.response_cache", MapResponseCache(db_path=tmp_path / "api-cache.sqlite3"))
    monkeypatch.setattr("app.hub_discovery.hub_store", HubStore(tmp_path / "hubs.sqlite3"))
    monkeypatch.delenv("OSRM_BASE_URL", raising=False)
    monkeypatch.delenv("CANDIDATE_STRATEGY", raising=False)

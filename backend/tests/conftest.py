import pytest

from app.map_cache import MapResponseCache


@pytest.fixture(autouse=True)
def isolated_map_cache(monkeypatch, tmp_path):
    # Mocked map data must never enter the running application's persistent cache.
    monkeypatch.setattr("app.amap.response_cache", MapResponseCache(db_path=tmp_path / "api-cache.sqlite3"))

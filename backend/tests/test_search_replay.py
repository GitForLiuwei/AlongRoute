import asyncio
import json

import httpx
import pytest

from app import search_log
from app.main import app
from app.search_replay import read_replay

IDENTIFIER = 'a' * 32


def write(path, records):
    path.write_text('\n'.join(json.dumps(dict(search_id=IDENTIFIER, kind='route', **r)) for r in records) + '\n', encoding='utf-8')


def test_rotated_interleaved_replay_keeps_constraints_and_only_allowed_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(search_log, 'LOG_DIR', tmp_path)
    key = ['114.054300,22.677599', '114.0,22.6', '114.05,22.67']
    write(tmp_path / '2026-11-06.jsonl.1', [
        dict(event='candidate_generated', sequence=2, candidate_key=key, source='roadside', secret_extra='do-not-export'),
        dict(event='route_input', sequence=1, request={'origin': {'name': 'A'}}),
        dict(event='map_request_finished', sequence=3, endpoint='private-url'),
    ])
    write(tmp_path / '2026-11-06.jsonl', [
        dict(event='candidate_preselection', sequence=4, candidate_key=key, decision='quota_limit'),
        dict(event='route_result', sequence=5, recommendations=[], search_complete=False),
        dict(event='search_finished', sequence=6, status='success'),
    ])
    with (tmp_path / '2026-11-06.jsonl').open('a') as stream:
        stream.write(json.dumps(dict(search_id='b' * 32, kind='route', event='route_input', sequence=1, request={'origin': 'other user'})) + '\n')
        stream.write('{interrupted')
    replay = read_replay(IDENTIFIER)
    assert replay['finished']
    assert [e['sequence'] for e in replay['events']] == [1, 2, 4, 5, 6]
    assert replay['events'][1]['candidate_key'] == key
    assert replay['events'][2]['decision'] == 'quota_limit'
    assert replay['events'][3]['search_complete'] is False
    assert all(value not in json.dumps(replay) for value in ('do-not-export', 'private-url', 'other user'))


def test_partial_and_missing_replays(tmp_path, monkeypatch):
    monkeypatch.setattr(search_log, 'LOG_DIR', tmp_path)
    write(tmp_path / '2026-11-06.jsonl', [dict(event='route_input', sequence=1, request={})])
    assert not read_replay(IDENTIFIER)['finished']
    assert read_replay('b' * 32) is None
    for identifier in ('../secrets', 'latest', 'A' * 32):
        with pytest.raises(ValueError):
            read_replay(identifier)


def test_endpoint_statuses_and_no_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(search_log, 'LOG_DIR', tmp_path)
    write(tmp_path / '2026-11-06.jsonl', [dict(event='route_input', sequence=1, request={})])
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            ok = await client.get(f'/api/search-replay/{IDENTIFIER}')
            assert ok.status_code == 200
            assert ok.headers['cache-control'] == 'no-store'
            assert (await client.get('/api/search-replay/' + 'b' * 32)).status_code == 404
            assert (await client.get('/api/search-replay/latest')).status_code == 422
    asyncio.run(run())

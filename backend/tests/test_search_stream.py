"""Realtime replay must work before POST completes and independently of disk logging."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import search_log
from app.main import app
from app.search_replay import read_replay, stream_replay
from test_search_log import payload


@pytest.fixture
def isolated_traces(monkeypatch, tmp_path):
    monkeypatch.setattr(search_log, '_ACTIVE_TRACES', search_log.OrderedDict())
    monkeypatch.setattr(search_log, 'LOG_DIR', tmp_path)
    monkeypatch.delenv('AMAP_WEB_SERVICE_KEY', raising=False)


def decode(frames):
    return [json.loads(frame.removeprefix('data: ').strip()) for frame in frames if frame.startswith('data: ')]


def test_pre_registered_stream_is_claimed_by_post_and_cannot_merge_requests(isolated_traces):
    identifier = uuid4().hex
    trace = search_log.get_or_create_trace(identifier)
    with TestClient(app) as client:
        response = client.post('/api/recommend', headers={'X-Search-ID': identifier}, json=payload())
        assert response.status_code == 200
        assert response.headers['X-Search-ID'] == response.json()['search_id'] == identifier
        assert search_log.get_active_trace(identifier) is trace
        assert trace.snapshot()[-1]['event'] == 'search_finished'
        repeated = client.post('/api/recommend', headers={'X-Search-ID': identifier}, json=payload())
        assert repeated.json()['search_id'] != identifier
        invalid = client.post('/api/recommend', headers={'X-Search-ID': 'z' * 32}, json=payload())
        assert invalid.json()['search_id'] not in (identifier, 'z' * 32)
        place = client.get('/api/places/suggest', params={'q': '广州'}, headers={'X-Search-ID': identifier})
        assert place.headers['X-Search-ID'] != identifier


def test_stream_snapshot_then_threaded_events_are_ordered_without_gaps(isolated_traces):
    async def run():
        trace = search_log.get_or_create_trace(uuid4().hex)
        trace.emit('candidate_generated', candidate_key=['113,23', '', ''])
        trace.emit('map_request_finished', endpoint='do-not-export')
        stream = stream_replay(trace)
        assert await anext(stream) == ': connected\n\n'
        first = await anext(stream)
        assert decode([first])[0]['event'] == 'candidate_generated'
        # Publish concurrently while the consumer is transitioning from snapshot to queue.
        def emit(index):
            trace.emit('candidate_started', candidate_key=[f'113,{index}', '', ''], secret_extra='hidden')
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(emit, range(20)))
        trace.emit('search_finished', status='success')
        rest = [frame async for frame in stream]
        events = decode([first, *rest])
        assert [e['sequence'] for e in events] == [1, *range(3, 24)]
        assert rest[-1].startswith('event: done\n')
        assert 'hidden' not in ''.join(rest) and 'do-not-export' not in ''.join(rest)
        assert trace._subscribers == []
    asyncio.run(run())


def test_live_replay_survives_disk_failure_and_redacts_fields(isolated_traces, monkeypatch):
    def unavailable(path):
        raise OSError('disk unavailable')
    monkeypatch.setattr(search_log, '_logger', unavailable)
    monkeypatch.setenv('AMAP_WEB_SERVICE_KEY', 'test-private-key')
    trace = search_log.get_or_create_trace(uuid4().hex)
    trace.emit('candidate_rejected', point={'name': 'test-private-key'}, reason='map_service_error',
               driver_polyline=[{'lon': 113, 'lat': 23}], secret_extra='do-not-export')
    replay = read_replay(trace.id)
    assert not replay['finished'] and trace.write_failed
    assert replay['events'][0]['point']['name'] == '[REDACTED]'
    assert 'secret_extra' not in replay['events'][0]
    assert 'test-private-key' not in json.dumps(trace.snapshot())


def test_disconnect_cancellation_and_generator_close_remove_subscribers(isolated_traces):
    class Disconnected:
        async def is_disconnected(self):
            return True

    async def run():
        for mode in ('disconnect', 'cancel', 'close'):
            trace = search_log.get_or_create_trace(uuid4().hex)
            stream = stream_replay(trace, Disconnected() if mode == 'disconnect' else None)
            await anext(stream)
            assert len(trace._subscribers) == 1
            if mode == 'disconnect':
                assert [frame async for frame in stream] == []
            elif mode == 'cancel':
                task = asyncio.create_task(anext(stream))
                await asyncio.sleep(0)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                await stream.aclose()
            assert trace._subscribers == []
    asyncio.run(run())


def test_completed_stream_headers_filtering_and_validation(isolated_traces):
    trace = search_log.get_or_create_trace(uuid4().hex)
    trace.emit('candidate_accepted', recommendation={'id': 'test'}, secret_extra='hidden')
    trace.emit('search_finished', status='success')
    with TestClient(app) as client:
        response = client.get(f'/api/search-replay/{trace.id}/stream')
        assert response.status_code == 200
        assert response.headers['content-type'].startswith('text/event-stream')
        assert response.headers['cache-control'] == 'no-store'
        assert response.headers['x-accel-buffering'] == 'no'
        assert 'hidden' not in response.text
        assert 'event: done' in response.text
        assert client.get('/api/search-replay/invalid/stream').status_code == 422


def test_registry_preserves_running_traces_and_bounds_recent_history(isolated_traces, monkeypatch):
    monkeypatch.setattr(search_log, '_MAX_ACTIVE_TRACES', 2)
    running, token = search_log.start_trace('route')
    search_log.reset_trace(token)
    completed, token = search_log.start_trace('route')
    search_log.reset_trace(token)
    completed.emit('search_finished', status='success')
    next_trace = search_log.get_or_create_trace(uuid4().hex)
    assert search_log.get_active_trace(running.id) is running
    assert search_log.get_active_trace(completed.id) is None
    assert len(search_log._ACTIVE_TRACES) == 2
    # An attached stream cannot be evicted between pre-registration and POST.
    async def run():
        _, queue = next_trace.subscribe(asyncio.get_running_loop())
        try:
            with pytest.raises(search_log.TraceCapacityError):
                search_log.get_or_create_trace(uuid4().hex)
        finally:
            next_trace.unsubscribe(queue)
    asyncio.run(run())

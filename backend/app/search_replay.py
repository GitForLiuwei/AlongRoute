"""Read one unguessable search ID; never enumerate other users' journeys."""
import asyncio
from collections import deque
import json
import re

from . import search_log

# Only planning diagnostics are exposed; map requests and arbitrary log fields stay local.
FIELDS = {
    'route_input': ('request', 'mode'),
    'discovery_map': ('driver_routes', 'passenger_routes', 'search_centers', 'search_radius_m', 'strategy'),
    'hub_seeds': ('hub_kind', 'center', 'cache', 'places'),
    'hub_shortlist': ('generated', 'selected'),
    'hub_exit_supplement': ('count',),
    'candidate_coarse': ('point', 'estimate'),
    'closest_route_point': ('point', 'controlled', 'distance_to_destination_m'),
    'exits_around_q': ('q', 'exits'),
    'destination_exits_selected': ('exits',),
    'exit_split_discovered': ('exit', 'split_point', 'candidates', 'discovery_group'),
    'candidate_generated': (),
    'candidate_preselection': ('decision',),
    'candidate_started': ('point',),
    'candidate_rejected': ('point', 'reason', 'driver_detour_s', 'limit_s', 'driver_polyline', 'passenger_polyline'),
    'candidate_accepted': ('recommendation', 'driver_polyline', 'passenger_polyline'),
    'candidate_not_evaluated': ('reason',),
    'preselection_summary': ('generated', 'unique', 'selected', 'limit', 'surface_reserved'),
    'taxi_filtered': ('candidate_id', 'reason', 'by_candidate_id'),
    'transit_filtered': ('candidate_id', 'reason', 'by_candidate_id'),
    'route_result': ('recommendations', 'search_complete', 'warnings', 'examined_candidates', 'mode'),
    'search_finished': ('status',),
}
TRACE_FIELDS = ('candidate_key', 'route_index', 'region', 'direction', 'sample_tier',
                'origins', 'source', 'discovery_group', 'hub_kind')


def format_replay_event(record):
    name = record.get('event')
    if record.get('kind') != 'route' or name not in FIELDS:
        return None
    allowed = ('event', 'sequence', 'elapsed_ms', 'time', *FIELDS[name], *TRACE_FIELDS)
    return search_log._redact({key: record[key] for key in allowed if key in record})


def read_replay(identifier):
    if not re.fullmatch(r'[a-f0-9]{32}', identifier):
        raise ValueError('搜索编号应为32位小写十六进制字符')
    active = search_log.get_active_trace(identifier)
    if active is not None:
        records = active.snapshot()
        if records:
            events = [item for record in records if (item := format_replay_event(record)) is not None]
            return dict(search_id=identifier, events=events,
                        finished=any(e['event'] == 'search_finished' for e in events))
    events = []
    found_route = False
    # Include rotated files; sorting by sequence later restores request order.
    for path in sorted(search_log.LOG_DIR.glob('*.jsonl*')):
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}\.jsonl(?:\.[1-3])?', path.name) or not path.is_file():
            continue
        with path.open(encoding='utf-8', errors='replace') as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeError):
                    continue  # an interrupted final line must not hide a whole replay
                if not isinstance(record, dict) or record.get('search_id') != identifier or record.get('kind') != 'route':
                    continue
                found_route = True
                item = format_replay_event(record)
                if item is not None:
                    events.append(item)
    if not found_route:
        return None
    events.sort(key=lambda e: e.get('sequence', 0))
    return search_log._redact(dict(search_id=identifier, events=events,
        finished=any(e['event'] == 'search_finished' for e in events)))


async def stream_replay(trace, request=None):
    """Send the snapshot and subsequent events, with cleanup on disconnect/cancel."""
    loop = asyncio.get_running_loop()
    snapshot, queue = trace.subscribe(loop)
    snapshot = deque(snapshot)
    deadline = loop.time() + 120  # Planning's server timeout is 110 seconds.
    try:
        # Flush headers immediately, even if the POST has not arrived yet.
        yield ': connected\n\n'
        while True:
            if snapshot:
                record = snapshot.popleft()
            else:
                if loop.time() >= deadline or (request is not None and await request.is_disconnected()):
                    return
                try:
                    record = await asyncio.wait_for(queue.get(), timeout=1)
                except asyncio.TimeoutError:
                    yield ': keep-alive\n\n'
                    continue
            item = format_replay_event(record)
            if item is not None:
                yield f'data: {json.dumps(item, ensure_ascii=False, allow_nan=False)}\n\n'
            if record.get('event') == 'search_finished':
                yield 'event: done\ndata: {"finished": true}\n\n'
                return
    finally:
        trace.unsubscribe(queue)

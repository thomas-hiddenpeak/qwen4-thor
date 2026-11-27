"""Correlate the native soak's guard scans, NVML worker replies and model identity."""
import json
import math

from tools.serve_soak.evidence import require


def records(path):
    with path.open('rb') as stream:
        while raw := stream.readline(8 * 1024**2 + 1):
            require(len(raw) <= 8 * 1024**2 and raw.endswith(b'\n'),
                    'incomplete or oversized observation')
            row = json.loads(raw)
            require(isinstance(row, dict), 'observation must be an object')
            yield row


def timestamp(row):
    value = row.get('monotonic')
    require(type(value) in (float, int) and math.isfinite(value), 'invalid observation time')
    return value


def ready_windows(rows, initial):
    pid, ticks = initial['model_pid'], initial['model_start_ticks']
    require(all(type(value) is int and value > 0 for value in (pid, ticks)),
            'missing initial model identity')
    begin = previous = None
    for row in rows:
        now = timestamp(row)
        require(previous is None or now >= previous, 'status clock regressed')
        require(type(row.get('business_ready')) is bool, 'invalid readiness observation')
        if row['business_ready']:
            require(row['model_pid'] == pid and row['model_start_ticks'] == ticks,
                    'another model was ready')
            if begin is None:
                begin = now
        elif begin is not None:
            yield begin, previous
            begin = None
        previous = now
    if begin is not None:
        yield begin, previous


def worker_replies(rows):
    previous = -math.inf
    for query in rows:
        require(query['status'] == 'OK' and query['returncode'] == 0 and
                isinstance(query['request_id'], str) and query['request_id'],
                'unsuccessful NVML query')
        events = [json.loads(line) for line in query['stdout'].splitlines()]
        require(events and all(event['request_id'] == query['request_id'] for event in events),
                'NVML worker request identity differs')
        final = events[-1]
        require(final['event'] == 'result' and final['status'] == 'OK' and
                all(e['event'] not in ('failure', 'result') for e in events[:-1]) and
                type(final['device_count']) is int and final['device_count'] > 0 and
                final['device_count'] == query['device_count'] and
                isinstance(final['pids'], list) and
                all(type(pid) is int and pid > 0 for pid in final['pids']) and
                sorted(set(final['pids'])) == query['pids'] and
                final.get('process_memory') == query.get('process_memory'),
                'NVML query summary differs from worker result')
        now = timestamp(final)
        require(now > previous, 'NVML worker reply clock regressed')
        previous = now
        yield now, final['pids']


def verify(output, lease, initial):
    require(lease['compute_backend'] == 'nvml' and lease['status'] == 'COMPLETED'
            and lease['child_returncode'] == 0 and lease['lease_inherited_by_child'],
            'expected a completed inherited NVML lease')
    replies = worker_replies(records(output/'capture/nvml-queries.jsonl'))
    windows = ready_windows(records(output/'execution/status.jsonl'), initial)
    reply = next(replies, None)
    window = next(windows, None)
    scans = queries = model_queries = ready_queries = 0
    boundary = None
    released = False
    for event in records(output/'capture/events.jsonl'):
        now = timestamp(event)
        kind = event['event']
        require(not released, 'guard event after release')
        if boundary is None:
            require(kind == 'lease_acquired', 'missing initial lease boundary')
            boundary = now
        else:
            require(kind != 'lease_acquired' and now >= boundary, 'invalid guard scan boundary')
        if kind in ('preflight', 'monitor'):
            require((kind == 'preflight') == (scans == 0) and not event['conflicts'],
                    'unexpected strict guard scan')
            count = 0
            while reply is not None and reply[0] <= now:
                require(count < 2, 'too many NVML replies for one scan')
                at, pids = reply
                require(at >= boundary, 'NVML reply outside its scan')
                while window is not None and at > window[1]:
                    window = next(windows, None)
                if initial['model_pid'] in pids:
                    model_queries += 1
                    if window is not None and window[0] <= at <= window[1]:
                        ready_queries += 1
                count += 1
                reply = next(replies, None)
            # inspect() can make a second query when an owned CUDA process exits.
            require(1 <= count <= 2 and (not event.get('retired_owned_contexts') or count == 2),
                    'strict guard scan missing or exceeding its worker replies')
            scans += 1
            queries += count
            boundary = now
        elif kind == 'lease_released':
            require(event['status'] == 'COMPLETED' and reply is None, 'unfinished NVML queries')
            released = True
    # Check later status identities too, even when the last query preceded them.
    for _ in windows:
        pass
    require(released and scans >= 2 and ready_queries > 0,
            'no completed guard with an observed ready model')
    return dict(guard_scans=scans, nvml_queries=queries, additional_queries=queries-scans,
                model_context_queries=model_queries, model_context_queries_during_ready=ready_queries)

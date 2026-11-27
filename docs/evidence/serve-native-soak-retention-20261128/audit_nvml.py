"""Cross-check archived NVML worker replies against the native soak guard and model."""
import argparse
import hashlib
import json
import math
from pathlib import Path


def require(ok, message):
    if not ok:
        raise ValueError(message)


def read(path):
    return json.loads(path.read_bytes())


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def records(path):
    with path.open('rb') as stream:
        while raw := stream.readline(8 * 1024**2 + 1):
            require(len(raw) <= 8 * 1024**2 and raw.endswith(b'\n'),
                    'incomplete or oversized observation')
            yield json.loads(raw)


def timestamp(row):
    value = row['monotonic']
    require(type(value) in (float, int) and math.isfinite(value), 'invalid monotonic time')
    return value


def worker_results(rows):
    previous = float('-inf')
    for query in rows:
        events = [json.loads(line) for line in query['stdout'].splitlines()]
        require(query['status'] == 'OK' and query['returncode'] == 0 and events,
                'unsuccessful NVML query')
        require(all(event['request_id'] == query['request_id'] for event in events),
                'NVML worker request ID differs')
        final = events[-1]
        require(final['event'] == 'result' and final['status'] == 'OK' and
                final['device_count'] == query['device_count'] > 0 and
                sorted(set(final['pids'])) == query['pids'] and
                final.get('process_memory') == query.get('process_memory'),
                'query summary differs from the original NVML reply')
        now = timestamp(final)
        require(now > previous, 'NVML replies out of order')
        previous = now
        yield final


def check_scans(events, replies, pid, start, end):
    replies = iter(worker_results(replies))
    query = next(replies, None)
    boundary = None
    released = False
    samples = extra = context_samples = ready_samples = 0
    for event in events:
        kind = event['event']
        now = timestamp(event)
        require(not released, 'unexpected event after lease release')
        if boundary is not None:
            require(kind != 'lease_acquired' and now >= boundary, 'guard scan time or boundary regressed')
        if boundary is None:
            require(kind == 'lease_acquired', 'missing initial lease boundary')
            boundary = now
        if kind in ('preflight', 'monitor'):
            require((kind == 'preflight') == (samples == 0) and not released and
                    event['conflicts'] == [], 'invalid guard scan')
            count = 0
            while query is not None and timestamp(query) <= now:
                at = timestamp(query)
                require(at >= boundary, 'query outside its guard scan')
                if pid in query['pids']:
                    context_samples += 1
                    ready_samples += int(start <= at <= end)
                count += 1
                query = next(replies, None)
            # inspect() can query again to resolve a recently exited owned PID.
            require(1 <= count <= 2, 'guard scan lacks its original NVML query or has excess replies')
            samples += 1
            extra += count - 1
            boundary = now
        elif kind == 'lease_released':
            require(not released and event['status'] == 'COMPLETED' and
                    query is None, 'unfinished queries at lease release')
            released = True
    require(released and samples >= 2 and ready_samples > 0,
            'missing completed lease or model NVML observation while ready')
    return dict(guard_scans=samples, nvml_queries=samples+extra,
                additional_queries=extra, model_context_queries=context_samples,
                model_context_queries_during_ready=ready_samples)


def audit(output):
    seal = read(output/'seal.json')['files']
    inputs = ('lease/exit.json', 'capture/events.jsonl', 'capture/nvml-queries.jsonl',
              'execution/initial-ready.json', 'execution/status.jsonl')
    for name in inputs:
        require(sha(output/name) == seal[name], 'sealed input differs: '+name)
    lease = read(output/inputs[0])
    require(lease['status'] == 'COMPLETED' and lease['child_returncode'] == 0 and
            lease['compute_backend'] == 'nvml' and lease['lease_inherited_by_child'],
            'strict native GPU lease did not complete')
    initial = read(output/inputs[3])
    pid, ticks = initial['model_pid'], initial['model_start_ticks']
    require(type(pid) is int and pid > 0 and type(ticks) is int and ticks > 0,
            'initial model identity missing')
    start = end = None
    for row in records(output/inputs[4]):
        if row.get('business_ready'):
            require((row['model_pid'], row['model_start_ticks']) == (pid, ticks),
                    'ready observations changed model identity')
            now = timestamp(row)
            if start is None:
                start = now
            require(end is None or now >= end, 'ready observation time regressed')
            end = now
    require(start is not None and end > start, 'ready observation window missing')
    return dict(check_scans(records(output/inputs[1]), records(output/inputs[2]), pid, start, end),
                original_seal_sha256=sha(output/'seal.json'), model_pid=pid,
                model_start_ticks=ticks, ready_observation_seconds=end-start)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    print(json.dumps(audit(parser.parse_args().output), indent=2))

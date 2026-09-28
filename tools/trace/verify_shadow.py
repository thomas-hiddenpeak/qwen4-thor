"""Independent ordered-list replay of all online shadow request records."""
import argparse
from collections import Counter
import json
from pathlib import Path
import struct
from analyze import analyze, frames, sha

SLOT = 2765056


def verify(directory, output, checker, binary):
    status = json.loads((output / 'status.json').read_text())
    if not status['complete'] or not status['saw_open_run']:
        raise ValueError('not a completed live observation')
    validated = analyze(directory, checker, binary)
    ranks = json.loads((output / 'calibration.json').read_text())['rankings']
    states, comparisons = {}, 0
    for rid, source in enumerate(validated['requests'], 1):
        recorded = json.loads((output / f'request-{rid}.json').read_text())
        assert recorded['trace_sha256'] == source['sha256']
        reports = {}
        for cap in [32, 64]:
            for policy in ['static', 'lru']:
                for mode in ['prefill_reset', 'continuous']:
                    key = (cap, policy, mode)
                    initialize = key not in states or mode == 'prefill_reset'
                    if initialize:
                        states[key] = [rank[:cap] if policy == 'static' else [] for rank in ranks]
                    reports[key] = dict(initial_bytes=48 * cap * SLOT if initialize and policy == 'static' else 0,
                                        layers={})
        events = frames(directory / source['file'])
        next(events)
        pending = []
        for payload in events:
            kind = struct.unpack('<I', payload[:4])[0]
            body = payload[12:]
            if kind == 2:
                _, stage, _, _ = struct.unpack('<QIQI', body)
                pending = []
            elif kind == 3:
                layer = struct.unpack('<I', body[:4])[0]
                pending.append((layer, [v[0] for v in struct.iter_unpack('<H', body[4:])]))
            elif kind == 4 and body == b'\1\1\1':
                for layer, values in pending:
                    counts, needed = Counter(values), set(values)
                    for key, resident_layers in states.items():
                        cap, policy, _ = key
                        resident = resident_layers[layer]
                        hit = needed.intersection(resident)
                        missing = len(needed) - len(hit)
                        if policy == 'lru' and len(needed) <= cap:
                            resident = ([e for e in resident if e not in needed] + sorted(needed))[-cap:]
                            resident_layers[layer] = resident
                        row = reports[key]['layers'].setdefault((stage, layer), dict(
                            groups=0, demands=0, routes=0, hits=0, route_hits=0,
                            misses=0, full_hits=0, logical_bytes=0, oversized=0,
                            occupancy_bytes=0, missing_histogram={}))
                        values_to_add = dict(groups=1, demands=len(needed), routes=len(values),
                            hits=len(hit), route_hits=sum(counts[e] for e in hit), misses=missing,
                            full_hits=int(missing == 0), logical_bytes=missing * SLOT,
                            oversized=int(len(needed) > cap))
                        for k, v in values_to_add.items():
                            row[k] += v
                        row['occupancy_bytes'] = max(row['occupancy_bytes'], len(resident) * SLOT)
                        hist = row['missing_histogram']
                        hist[str(missing)] = hist.get(str(missing), 0) + 1
        for actual in recorded['experiments']:
            key = tuple(actual[k] for k in ['capacity', 'policy', 'mode'])
            expected = reports[key]
            assert actual['initial_bytes'] == expected['initial_bytes']
            assert actual['total_budget_bytes'] == (48 * key[0] + 10) * SLOT
            assert len(actual['layers']) == len(expected['layers'])
            for row in actual['layers']:
                comparison = {k: v for k, v in row.items() if k not in ['stage', 'layer']}
                assert comparison == expected['layers'][(row['stage'], row['layer'])]
                comparisons += 1
        for stage, key in [(1, 'committed_prefill_rows'), (2, 'committed_decode_rows')]:
            count = sum(x['routes'] for x in recorded['experiments'][0]['layers'] if x['stage'] == stage)
            assert count == source['verified'][key] * 480
    assert len(validated['requests']) == status['requests']
    return dict(passed=True, requests=status['requests'], layer_policy_comparisons=comparisons,
                status_sha256=sha(output / 'status.json'), complete_source_validation=True,
                successful_requests=validated['successful_requests'], excluded_requests=validated['excluded_requests'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ['directory', 'output', 'checker', 'binary']:
        parser.add_argument('--' + arg, type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.directory.resolve(), args.output.resolve(),
                            args.checker.resolve(), args.binary.resolve()), indent=2))

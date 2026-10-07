"""Independent static and cold-LRU decode recomputation from raw trace IDs."""
import argparse
from array import array
from collections import Counter
import json
from pathlib import Path
import struct
import sys
from analyze import frames, sha


def decode(path):
    layers = [[] for _ in range(48)]
    events = frames(path)
    next(events)
    n, stage = 0, 0
    for payload in events:
        kind = struct.unpack('<I', payload[:4])[0]
        body = payload[12:]
        if kind == 2:
            _, stage, _, _ = struct.unpack('<QIQI', body)
            n += stage == 2
        if kind == 3 and stage == 2 and n <= 64:
            layer = struct.unpack('<I', body[:4])[0]
            values = array('H')
            values.frombytes(body[4:])
            if sys.byteorder != 'little':
                values.byteswap()
            layers[layer].append(list(values))
    assert all(len(x) == 64 for x in layers)
    return layers


def verify(result):
    plan = result['plan']
    def read(item):
        return decode(Path(plan['sources'][item['source']]) / item['file'])
    train = [Counter() for _ in range(48)]
    for item in plan['calibration']:
        for layer, rows in enumerate(read(item)):
            for row in rows:
                train[layer].update(row)
    ranks = [sorted(range(512), key=lambda e: (-h[e], e)) for h in train]
    assert ranks == result['rankings']
    checks = 0
    for cohort, items in plan['evaluation'].items():
        for item in items:
            data = read(item)
            for capacity in plan['capacities']:
                for policy in ('static', 'lru'):
                    output = next(x for x in result['results'] if
                                  (x['cohort'], x['capacity'], x['policy'], x['mode']) ==
                                  (cohort, capacity, policy, 'cold_decode'))
                    request = next(x for x in output['requests'] if x['name'] == item['name'])
                    for layer, rows in enumerate(data):
                        resident = ranks[layer][:capacity] if policy == 'static' else []
                        hits, full, missing = 0, 0, Counter()
                        for row in rows:
                            h = sum(e in resident for e in row)
                            hits += h
                            full += h == 10
                            missing[10 - h] += 1
                            if policy == 'lru':
                                old = [e for e in resident if e not in row]
                                resident = (old + sorted(row))[-capacity:]
                        reported = next(x for x in request['layers'] if x['layer'] == layer)
                        assert (hits, full, dict(missing)) == (
                            reported['hits'], reported['full_hits'],
                            {int(k): v for k, v in reported['missing_histogram'].items()})
                        assert reported['logical_bytes'] == (640 - hits) * result['slot_bytes']
                        checks += 1
    for output in result['results']:
        for request in output['requests']:
            for row in request['layers']:
                assert sum(row['missing_histogram'].values()) == row['groups']
                assert sum(int(k) * v for k, v in row['missing_histogram'].items()) == row['misses']
                assert row['hits'] + row['misses'] == row['demands']
                assert row['logical_bytes'] == row['misses'] * result['slot_bytes']
    return dict(passed=True, independently_checked_request_layer_policies=checks,
                scope='all cold decode static/LRU; all modes byte/histogram conservation')


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--results', type=Path, required=True)
    args = ap.parse_args()
    print(json.dumps(dict(results_sha256=sha(args.results),
                          **verify(json.loads(args.results.read_text()))), indent=2))

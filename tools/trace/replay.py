"""Causal, fixed per-layer cache replay. See README for execution assumptions."""
import argparse
from array import array
from collections import Counter
import json
from pathlib import Path
import struct
import sys
import subprocess

from analyze import analyze, frames, sha


# Packed gate/up, down, atom-padded SF, four FP32 scales; slot aligned to 256.
EXPERT_PAYLOAD = 640 * 2560 + 2560 * 320 + 10 * 40 * 512 + 20 * 10 * 512 + 16
SLOT_BYTES = (EXPERT_PAYLOAD + 255) // 256 * 256
LAYERS, EXPERTS, TOP_K = 48, 512, 10


def groups(path, window):
    events = frames(path)
    next(events)
    decode = 0
    for payload in events:
        kind = struct.unpack('<I', payload[:4])[0]
        body = payload[12:]
        if kind == 2:
            _, stage, position, rows = struct.unpack('<QIQI', body)
            if stage == 2:
                decode += 1
        elif kind == 3 and (stage == 1 or decode <= window):
            layer = struct.unpack('<I', body[:4])[0]
            ids = array('H')
            ids.frombytes(body[4:])
            if sys.byteorder != 'little':
                ids.byteswap()
            yield stage, layer, position, Counter(ids)


class Cache:
    """All hit decisions precede mutation; same-group recency ties use ID."""
    def __init__(self, capacity, policy, ranking):
        if not TOP_K <= capacity <= EXPERTS or policy not in ('static', 'lru'):
            raise ValueError('invalid capacity or policy')
        if len(ranking) != EXPERTS or set(ranking) != set(range(EXPERTS)):
            raise ValueError('invalid calibration ranking')
        self.capacity, self.policy = capacity, policy
        self.resident = ({e: 0 for e in ranking[:capacity]}
                         if policy == 'static' else {})
        self.initial = len(self.resident)
        self.clock = 0

    def consume(self, counts, stage):
        need = set(counts)
        if (not need or not need <= set(range(EXPERTS)) or
                any(n <= 0 for n in counts.values()) or
                stage not in (1, 2) or
                (stage == 2 and (len(need) != TOP_K or
                                 sum(counts.values()) != TOP_K))):
            raise ValueError('invalid demand group')
        hits = need & self.resident.keys()
        missing = need - hits
        route_hits = sum(counts[e] for e in hits)
        self.clock += 1
        # Oversized prefill: retain cache, stream each missing expert once.
        # This explicitly bypasses admission; no arbitrary per-token LRU.
        if self.policy == 'lru' and len(need) <= self.capacity:
            victims = sorted(self.resident.keys() - need,
                             key=lambda e: (self.resident[e], e))
            remove = max(0, len(self.resident) + len(missing) - self.capacity)
            for e in victims[:remove]:
                del self.resident[e]
            for e in need:
                self.resident[e] = self.clock
            assert need <= self.resident.keys()
        assert len(self.resident) <= self.capacity
        return dict(groups=1, demands=len(need), routes=sum(counts.values()),
                    hits=len(hits), route_hits=route_hits, misses=len(missing),
                    full_hits=int(not missing),
                    logical_bytes=len(missing) * SLOT_BYTES,
                    oversized=int(len(need) > self.capacity),
                    occupancy_bytes=len(self.resident) * SLOT_BYTES)


def run(requests, ranks, capacity, policy, mode, window):
    if mode not in ('cold_decode', 'prefill_reset', 'continuous') or not requests:
        raise ValueError('empty requests or invalid mode')
    result, caches = [], None
    for request in requests:
        initial = 0
        if caches is None or mode != 'continuous':
            caches = [Cache(capacity, policy, rank) for rank in ranks]
            initial = sum(c.initial for c in caches) * SLOT_BYTES
        buckets = {}
        for stage, layer, position, counts in request['groups']:
            if mode == 'cold_decode' and stage == 1:
                continue
            row = caches[layer].consume(counts, stage)
            key = (stage, layer)
            if key not in buckets:
                buckets[key] = dict(stage=stage, layer=layer,
                                    missing_histogram={}, **{k: 0 for k in row})
            bucket = buckets[key]
            for k, v in row.items():
                if k == 'occupancy_bytes':
                    bucket[k] = max(bucket[k], v)
                else:
                    bucket[k] += v
            hist = bucket['missing_histogram']
            hist[row['misses']] = hist.get(row['misses'], 0) + 1
        if not any(s == 2 for s, _ in buckets):
            raise ValueError('empty decode selection')
        result.append(dict(name=request['name'], initial_bytes=initial,
                           layers=list(buckets.values())))
    return dict(capacity=capacity, policy=policy, mode=mode,
                cache_budget_bytes=LAYERS * capacity * SLOT_BYTES,
                staging_budget_bytes=TOP_K * SLOT_BYTES,
                total_budget_bytes=(LAYERS * capacity + TOP_K) * SLOT_BYTES,
                requests=result)


def validate_plan(plan):
    if plan.get('window') != 64 or plan.get('capacities') != [32, 64, 128, 256]:
        raise ValueError('unsupported frozen window/capacities')
    if not plan.get('sources') or not plan.get('calibration') or not plan.get('evaluation'):
        raise ValueError('empty sources/calibration/evaluation')
    seen = set()
    for split in [plan['calibration'], *plan['evaluation'].values()]:
        if not split:
            raise ValueError('empty split')
        names = set()
        for item in split:
            key = (item['source'], item['file'])
            if item['source'] not in plan['sources'] or key in seen:
                raise ValueError('unknown source or overlapping request')
            if item['name'] in names:
                raise ValueError('duplicate request label')
            if len(item['sha256']) != 64:
                raise ValueError('missing expected trace identity')
            seen.add(key)
            names.add(item['name'])


def execute(plan, checker, binary):
    validate_plan(plan)
    sources, identities = {}, {}
    for name, directory in plan['sources'].items():
        directory = Path(directory).resolve()
        if sha(directory / 'manifest.json') != plan['manifest_sha256'][name]:
            raise ValueError('source manifest changed')
        verified = analyze(directory, checker, binary)
        m = verified['manifest']
        if (m['layers'], m['experts'], m['top_k']) != (48, 512, 10):
            raise ValueError('unsupported model dimensions')
        config = json.loads((directory / 'model-config.json').read_text())['text_config']
        if (config['hidden_size'], config['moe_intermediate_size']) != (2560, 640):
            raise ValueError('unsupported weight layout')
        sources[name] = (directory, {r['file']: r for r in verified['requests']})
        identities[name] = verified
    if len({v['manifest']['model_index_sha256'] for v in identities.values()}) != 1:
        raise ValueError('mixed model identities')
    if len({v['manifest'].get('model_config_sha256') for v in identities.values()}) != 1:
        raise ValueError('mixed model configurations')
    used = set()

    def select(items):
        if not items:
            raise ValueError('empty split')
        selected = []
        for item in items:
            directory, available = sources[item['source']]
            metadata = available[item['file']]
            identity = metadata['sha256']
            if identity != item['sha256']:
                raise ValueError('selected trace changed')
            if identity in used or metadata['outcome'] != 'success':
                raise ValueError('duplicate, overlapping or unsuccessful request')
            used.add(identity)
            gs = list(groups(directory / item['file'], plan['window']))
            per_layer = Counter(l for s, l, p, c in gs if s == 2)
            if per_layer != Counter({l: 64 for l in range(LAYERS)}):
                raise ValueError('short decode window')
            selected.append(dict(**item, groups=gs))
        return selected

    training = select(plan['calibration'])
    counts = [Counter() for _ in range(LAYERS)]
    for request in training:
        for stage, layer, _, histogram in request['groups']:
            if stage == 2:
                counts[layer].update(histogram)
    ranks = [sorted(range(EXPERTS), key=lambda e: (-c[e], e)) for c in counts]
    cohorts = {name: select(items) for name, items in plan['evaluation'].items()}
    if not cohorts:
        raise ValueError('empty evaluation')
    outputs = []
    for name, requests in cohorts.items():
        for capacity in plan['capacities']:
            for policy in ('static', 'lru'):
                for mode in ('cold_decode', 'prefill_reset', 'continuous'):
                    outputs.append(dict(cohort=name, **run(
                        requests, ranks, capacity, policy, mode, plan['window'])))
    return dict(schema=1, plan=plan, sources=identities, rankings=ranks,
                payload_bytes=EXPERT_PAYLOAD, slot_bytes=SLOT_BYTES,
                results=outputs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--checker', type=Path, required=True)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        root = Path(__file__).resolve().parents[2]
        output = args.output.resolve()
        if not any(output.is_relative_to(root / p) for p in ('build', '.q4t-work')):
            raise ValueError('output must be in build/ or .q4t-work/')
        if output.exists():
            raise ValueError('output exists')
        tool_sha = sha(Path(__file__))
        result = execute(json.loads(args.plan.read_text()),
                         args.checker.resolve(), args.binary.resolve())
        result['tool_sha256'] = tool_sha
        result['plan_sha256'] = sha(args.plan)
        result['checker_sha256'] = sha(args.checker)
        with output.open('x') as file:
            json.dump(result, file, indent=2)
            file.write('\n')
    except (ValueError, OSError, KeyError, TypeError, StopIteration,
            subprocess.TimeoutExpired) as error:
        print(f'replay rejected: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())

"""Offline frozen-hotset plus LRU study; no model execution or future ranking."""
import argparse
import csv
import json
from pathlib import Path

from analyze import analyze, sha
from replay import SLOT_BYTES
from run_shadow_study import save
from shadow import committed_groups


class Hybrid:
    def __init__(self, capacity, pinned, rank):
        self.fixed = set(rank[:pinned])
        self.capacity = capacity - pinned
        self.dynamic = {}
        self.ordered = []  # Independent ordered-list state for direct checks.
        self.clock = 0

    def consume(self, counts):
        needed = set(counts)
        hit = needed & (self.fixed | self.dynamic.keys())
        assert hit == needed & (self.fixed | set(self.ordered))
        remaining = needed - self.fixed
        self.clock += 1
        if len(remaining) <= self.capacity:
            missing = remaining - self.dynamic.keys()
            victims = sorted(self.dynamic.keys() - remaining,
                             key=lambda e: (self.dynamic[e], e))
            n = max(0, len(self.dynamic) + len(missing) - self.capacity)
            for e in victims[:n]:
                del self.dynamic[e]
            for e in remaining:
                self.dynamic[e] = self.clock
            ordered = [e for e in self.ordered if e not in remaining] + sorted(remaining)
            self.ordered = ordered[-self.capacity:] if self.capacity else []
        assert sorted(self.dynamic, key=lambda e: (self.dynamic[e], e)) == self.ordered
        assert len(self.dynamic) <= self.capacity and not self.fixed.intersection(self.dynamic)
        return dict(groups=1, demands=len(needed), hits=len(hit),
                    fixed_hits=len(needed & self.fixed), routes=sum(counts.values()),
                    route_hits=sum(counts[e] for e in hit),
                    full_hits=int(needed <= hit), misses=len(needed - hit),
                    logical_bytes=len(needed - hit) * SLOT_BYTES)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    for name in ['directory', 'baseline', 'output', 'checker', 'binary']:
        ap.add_argument('--' + name, type=Path, required=True)
    args = ap.parse_args()
    directory, output = args.directory.resolve(), args.output.resolve()
    root = Path(__file__).resolve().parents[2]
    if not any(output.is_relative_to(root / p) for p in ['build', '.q4t-work']):
        ap.error('output must be under build/ or .q4t-work/')
    output.mkdir(parents=True, exist_ok=False)
    for base, file in [(directory, 'analysis/bindings.json'), (args.baseline, 'bindings.json')]:
        for name, digest in json.loads((base / file).read_text()).items():
            assert sha(base / name) == digest, name
    source = analyze(directory / 'trace', args.checker.resolve(), args.binary.resolve())
    ranks = json.loads((directory / 'frozen-calibration.json').read_text())
    assert ranks['model_index_sha256'] == source['manifest']['model_index_sha256']
    ranks = ranks['rankings']
    assert len(ranks) == 48 and all(sorted(r) == list(range(512)) for r in ranks)
    responses = json.loads((directory / 'responses.json').read_text())
    with (args.baseline / 'requests.csv').open() as file:
        baseline = {(int(r['request_id']), int(r['capacity']), r['policy'], r['mode']): r for r in csv.DictReader(file)}
    states, previous, results, comparisons = {}, [0], [], 0
    metrics = ['groups', 'demands', 'hits', 'fixed_hits', 'routes', 'route_hits', 'full_hits', 'misses', 'logical_bytes']
    for rid, request in enumerate(source['requests'], 1):
        assert request['request_id'] == rid and request['http_id'] == responses[rid - 1]['http_id']
        rows = {}
        for cap in [64, 128, 256]:
            for percent in [0, 25, 50, 75, 100]:
                fixed = cap * percent // 100
                for mode in ['prefill_reset', 'continuous']:
                    key = cap, percent, mode
                    reset = key not in states or mode == 'prefill_reset'
                    if reset:
                        states[key] = [Hybrid(cap, fixed, rank) for rank in ranks]
                    rows[key] = dict(request_id=rid, scenario=responses[rid - 1]['scenario'],
                        capacity=cap, fixed_percent=percent, fixed_slots=fixed, mode=mode,
                        initial_bytes=48 * fixed * SLOT_BYTES if reset else 0,
                        budget_bytes=(48 * cap + 10) * SLOT_BYTES,
                        **{stage + '_' + m: 0 for stage in ['prefill', 'decode'] for m in metrics})
        for stage, layer, counts in committed_groups(directory / 'trace' / request['file'], source['manifest'], rid, previous):
            label = 'prefill' if stage == 1 else 'decode'
            for key, caches in states.items():
                value = caches[layer].consume(counts)
                comparisons += 1
                for metric, n in value.items():
                    rows[key][label + '_' + metric] += n
        for (cap, percent, mode), row in rows.items():
            row['total_logical_bytes'] = row['initial_bytes'] + row['prefill_logical_bytes'] + row['decode_logical_bytes']
            if percent in [0, 100]:
                old = baseline[(rid, cap, 'lru' if percent == 0 else 'static', mode)]
                for field in ['initial_bytes', 'total_logical_bytes', 'budget_bytes'] + [stage + '_' + m for stage in ['prefill', 'decode'] for m in metrics if m != 'fixed_hits']:
                    assert row[field] == int(old[field]), (rid, cap, percent, field)
            results.append(row)
        print('replayed request', rid, flush=True)
    assert len(responses) == len(source['requests']) == 24
    with (output / 'requests.csv').open('x') as file:
        writer = csv.DictWriter(file, fieldnames=list(results[0]))
        writer.writeheader()
        writer.writerows(results)
    report = []
    for key in states:
        cap, percent, mode = key
        selected = [r for r in results if (r['capacity'], r['fixed_percent'], r['mode']) == key]
        total = dict(capacity=cap, fixed_percent=percent, mode=mode)
        for field in ['initial_bytes', 'total_logical_bytes'] + [stage + '_' + m for stage in ['prefill', 'decode'] for m in metrics]:
            total[field] = sum(r[field] for r in selected)
        total['decode_hit_percent'] = 100 * total['decode_hits'] / total['decode_demands']
        total['decode_full_percent'] = 100 * total['decode_full_hits'] / total['decode_groups']
        total['prefill_union_hit_percent'] = 100 * total['prefill_hits'] / total['prefill_demands']
        total['wins_over_lru'] = sum(r['total_logical_bytes'] < int(baseline[(r['request_id'], cap, 'lru', mode)]['total_logical_bytes']) for r in selected)
        report.append(total)
    save(output / 'summary.json', report)
    save(output / 'verification.json', dict(passed=True, group_policy_checks=comparisons,
        endpoint_rows_matched=24*3*2*2, requests=24, offline=True,
        source_bindings_sha256=sha(directory / 'analysis/bindings.json'),
        baseline_bindings_sha256=sha(args.baseline / 'bindings.json'),
        calibration_sha256=sha(directory / 'frozen-calibration.json'), tool_sha256=sha(Path(__file__))))


if __name__ == '__main__':
    main()

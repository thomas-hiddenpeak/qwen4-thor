"""Offline four-capacity replay of sealed EvalScope scenarios, no inference."""
import argparse
import csv
import json
from pathlib import Path

from analyze import analyze, sha
from replay import Cache, SLOT_BYTES
from run_shadow_study import save
from shadow import committed_groups
from summarize_shadow import summarize


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    for name in ['directory', 'output', 'checker', 'binary']:
        ap.add_argument('--' + name, type=Path, required=True)
    args = ap.parse_args()
    directory, output = args.directory.resolve(), args.output.resolve()
    root = Path(__file__).resolve().parents[2]
    if not any(output.is_relative_to(root / p) for p in ['build', '.q4t-work']):
        ap.error('output must be under build/ or .q4t-work/')
    output.mkdir(parents=True, exist_ok=False)
    bindings = json.loads((directory / 'analysis/bindings.json').read_text())
    for name, digest in bindings.items():
        if sha(directory / name) != digest:
            raise ValueError('sealed source changed: ' + name)
    source = analyze(directory / 'trace', args.checker.resolve(), args.binary.resolve())
    calibration = json.loads((directory / 'frozen-calibration.json').read_text())
    assert calibration['model_index_sha256'] == source['manifest']['model_index_sha256']
    ranks = calibration['rankings']
    assert len(ranks) == 48
    responses = json.loads((directory / 'responses.json').read_text())
    old_rows = {(r['request_id'], r['capacity'], r['policy'], r['mode']): r
                for r in summarize(directory / 'observer')}
    states, independent, previous, results = {}, {}, [0], []
    comparisons = 0
    for rid, request in enumerate(source['requests'], 1):
        assert request['request_id'] == rid
        response = responses[rid - 1]
        assert response['http_id'] == request['http_id']
        rows = {}
        for cap in [32, 64, 128, 256]:
            for policy in ['static', 'lru']:
                for mode in ['prefill_reset', 'continuous']:
                    key = cap, policy, mode
                    reset = key not in states or mode == 'prefill_reset'
                    if reset:
                        states[key] = [Cache(cap, policy, rank) for rank in ranks]
                        independent[key] = [rank[:cap] if policy == 'static' else [] for rank in ranks]
                    row = dict(request_id=rid, scenario=response['scenario'],
                               capacity=cap, policy=policy, mode=mode,
                               initial_bytes=48 * cap * SLOT_BYTES if reset and policy == 'static' else 0,
                               budget_bytes=(48 * cap + 10) * SLOT_BYTES)
                    for stage in ['prefill', 'decode']:
                        for metric in ['groups', 'demands', 'routes', 'hits', 'route_hits', 'misses', 'full_hits', 'logical_bytes', 'oversized']:
                            row[stage + '_' + metric] = 0
                    rows[key] = row
        for stage, layer, counts in committed_groups(directory / 'trace' / request['file'], source['manifest'], rid, previous):
            needed = set(counts)
            for key, caches in states.items():
                cap, policy, _ = key
                actual = caches[layer].consume(counts, stage)
                resident = independent[key][layer]
                hits = needed.intersection(resident)
                missing = len(needed) - len(hits)
                if policy == 'lru' and len(needed) <= cap:
                    resident = ([e for e in resident if e not in needed] + sorted(needed))[-cap:]
                    independent[key][layer] = resident
                expected = dict(groups=1, demands=len(needed), routes=sum(counts.values()),
                    hits=len(hits), route_hits=sum(counts[e] for e in hits), misses=missing,
                    full_hits=int(missing == 0), logical_bytes=missing * SLOT_BYTES,
                    oversized=int(len(needed) > cap), occupancy_bytes=len(resident) * SLOT_BYTES)
                assert actual == expected, (rid, key, stage, layer)
                comparisons += 1
                label = 'prefill' if stage == 1 else 'decode'
                for metric, value in actual.items():
                    if metric != 'occupancy_bytes':
                        rows[key][label + '_' + metric] += value
        for key, row in rows.items():
            row['total_logical_bytes'] = row['initial_bytes'] + row['prefill_logical_bytes'] + row['decode_logical_bytes']
            if key[0] in [32, 64]:
                old = old_rows[(rid, *key)]
                assert all(value == old[k] for k, value in row.items() if k != 'scenario')
            results.append(row)
    assert len(responses) == len(source['requests']) == 24
    with (output / 'requests.csv').open('x') as file:
        writer = csv.DictWriter(file, fieldnames=list(results[0]))
        writer.writeheader()
        writer.writerows(results)
    report = []
    for cap in [32, 64, 128, 256]:
        for mode in ['prefill_reset', 'continuous']:
            entry = dict(capacity=cap, mode=mode, budget_bytes=(48 * cap + 10) * SLOT_BYTES, policies={})
            policies = {}
            for policy in ['static', 'lru']:
                rows = [r for r in results if (r['capacity'], r['mode'], r['policy']) == (cap, mode, policy)]
                policies[policy] = rows
                totals = {k: sum(r[k] for r in rows) for k in ['decode_hits', 'decode_demands', 'decode_groups', 'decode_full_hits', 'initial_bytes', 'prefill_logical_bytes', 'decode_logical_bytes', 'total_logical_bytes']}
                totals['hit_percent'] = 100 * totals['decode_hits'] / totals['decode_demands']
                totals['full_percent'] = 100 * totals['decode_full_hits'] / totals['decode_groups']
                entry['policies'][policy] = totals
            entry['lru_total_wins'] = sum(l['total_logical_bytes'] < s['total_logical_bytes'] for s, l in zip(policies['static'], policies['lru']))
            report.append(entry)
    save(output / 'summary.json', report)
    save(output / 'verification.json', dict(passed=True, requests=24, group_policy_comparisons=comparisons,
         matched_previous_32_64=True, offline=True, source_bindings_sha256=sha(directory / 'analysis/bindings.json'),
         calibration_sha256=sha(directory / 'frozen-calibration.json'), tool_sha256=sha(Path(__file__))))
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()

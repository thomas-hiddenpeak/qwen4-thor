"""Verify the scenario capture and aggregate counts without a speed model."""
import argparse
import csv
import json
from pathlib import Path

from analyze import sha
from run_shadow_study import save
from summarize_shadow import summarize
from verify_shadow import verify


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    for key in ['directory', 'checker', 'binary']:
        ap.add_argument('--' + key, type=Path, required=True)
    args = ap.parse_args()
    directory = args.directory.resolve()
    root = Path(__file__).resolve().parents[2]
    if not any(directory.is_relative_to(root / p) for p in ['build', '.q4t-work']):
        ap.error('directory must be under build/ or .q4t-work/')
    output = directory / 'analysis'
    output.mkdir(exist_ok=False)
    verification = verify(directory / 'trace', directory / 'observer',
                          args.checker.resolve(), args.binary.resolve())
    save(output / 'verification.json', verification)
    responses = json.loads((directory / 'responses.json').read_text())
    rows = summarize(directory / 'observer')
    assert verification['requests'] == len(responses) == 24
    for row in rows:
        response = responses[row['request_id'] - 1]
        assert response['http_id'] == row['http_id']
        row.update({k: response[k] for k in ['scenario', 'variant', 'prompt_tokens', 'completion_tokens']})
    with (output / 'requests.csv').open('x') as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    report = []
    for scenario in ['all'] + sorted({r['scenario'] for r in rows}):
        for cap in [32, 64]:
            for mode in ['prefill_reset', 'continuous']:
                selected = [r for r in rows if r['capacity'] == cap and r['mode'] == mode
                            and (scenario == 'all' or r['scenario'] == scenario)]
                entry = dict(scenario=scenario, capacity=cap, mode=mode, policies={})
                for policy in ['static', 'lru']:
                    subset = [r for r in selected if r['policy'] == policy]
                    totals = {k: sum(r[k] for r in subset) for k in [
                        'decode_hits', 'decode_demands', 'decode_full_hits', 'decode_groups',
                        'decode_misses', 'initial_bytes', 'prefill_logical_bytes',
                        'decode_logical_bytes', 'total_logical_bytes']}
                    totals['hit_percent'] = 100 * totals['decode_hits'] / totals['decode_demands']
                    totals['full_percent'] = 100 * totals['decode_full_hits'] / totals['decode_groups']
                    entry['policies'][policy] = totals
                pairs = {policy: {r['request_id']: r for r in selected if r['policy'] == policy}
                         for policy in ['static', 'lru']}
                entry['requests'] = len(pairs['static'])
                for metric in ['decode_misses', 'total_logical_bytes']:
                    entry['lru_better_' + metric] = sum(
                        pairs['lru'][rid][metric] < r[metric] for rid, r in pairs['static'].items())
                report.append(entry)
    save(output / 'summary.json', report)
    save(output / 'bindings.json', {str(p.relative_to(directory)): sha(p)
         for p in sorted(directory.rglob('*')) if p.is_file() and p not in [output / 'bindings.json', directory / 'analysis.log']})
    print(json.dumps([r for r in report if r['scenario'] == 'all'], indent=2))


if __name__ == '__main__':
    main()

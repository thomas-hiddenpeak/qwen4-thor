"""Write reviewable request distributions and capacity curves; no speed model."""
import argparse
import csv
import json
from pathlib import Path


def summarize(result):
    requests, curves = [], []
    for experiment in result['results']:
        base = {k: experiment[k] for k in ('cohort', 'capacity', 'policy', 'mode')}
        local = []
        for request in experiment['requests']:
            row = dict(base, name=request['name'], initial_bytes=request['initial_bytes'])
            for stage, label in [(1, 'prefill'), (2, 'decode')]:
                layers = [x for x in request['layers'] if x['stage'] == stage]
                for metric in ['groups', 'demands', 'hits', 'routes', 'route_hits',
                               'misses', 'full_hits', 'logical_bytes', 'oversized']:
                    row[label + '_' + metric] = sum(x[metric] for x in layers)
            row['total_logical_bytes'] = (row['initial_bytes'] +
                row['prefill_logical_bytes'] + row['decode_logical_bytes'])
            requests.append(row)
            local.append(row)
        curve = dict(base, total_budget_bytes=experiment['total_budget_bytes'])
        for key in local[0]:
            if key not in base and key != 'name':
                curve[key] = sum(x[key] for x in local)
        curves.append(curve)
    return requests, curves


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--results', type=Path, required=True)
    ap.add_argument('--output-dir', type=Path, required=True)
    args = ap.parse_args()
    root = Path(__file__).resolve().parents[2]
    output = args.output_dir.resolve()
    if not any(output.is_relative_to(root / p) for p in ('build', '.q4t-work')):
        ap.error('output must be in build/ or .q4t-work/')
    request_rows, curves = summarize(json.loads(args.results.read_text()))
    for name, rows in [('requests.csv', request_rows), ('curves.csv', curves)]:
        with (output / name).open('x') as file:
            writer = csv.DictWriter(file, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

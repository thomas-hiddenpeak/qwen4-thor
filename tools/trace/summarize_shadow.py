"""Summarize completed bounded shadow observations without a speed model."""
import argparse
import csv
import json
from pathlib import Path


def summarize(directory):
    status = json.loads((directory / 'status.json').read_text())
    if not status['complete']:
        raise ValueError('incomplete observer')
    rows = []
    for rid in range(1, status['requests'] + 1):
        request = json.loads((directory / f'request-{rid}.json').read_text())
        for e in request['experiments']:
            row = dict(request_id=rid, http_id=request['metadata']['http_id'],
                capacity=e['capacity'], policy=e['policy'], mode=e['mode'],
                initial_bytes=e['initial_bytes'], budget_bytes=e['total_budget_bytes'],
                successful=request['verified']['successful_requests'],
                cancelled=request['verified']['cancelled_requests'],
                failed=request['verified']['failed_requests'])
            for stage, label in [(1, 'prefill'), (2, 'decode')]:
                layers = [x for x in e['layers'] if x['stage'] == stage]
                for metric in ['groups', 'routes', 'route_hits', 'demands', 'hits',
                               'misses', 'full_hits', 'logical_bytes', 'oversized']:
                    row[label + '_' + metric] = sum(x[metric] for x in layers)
            row['total_logical_bytes'] = row['initial_bytes'] + row['prefill_logical_bytes'] + row['decode_logical_bytes']
            rows.append(row)
    return rows


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--directory', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    root = Path(__file__).resolve().parents[2]
    if not any(args.output.resolve().is_relative_to(root / p) for p in ['build', '.q4t-work']):
        ap.error('output must be in build/ or .q4t-work/')
    rows = summarize(args.directory)
    with args.output.open('x') as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

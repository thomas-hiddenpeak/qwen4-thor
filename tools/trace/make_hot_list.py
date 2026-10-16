"""Build per-layer static hot lists from calibration traces.

Reads the frozen business set v1 plan and the calibration-split traces
(reuse + new runs, same mapping rules as capacity_curve.py) and writes a
moe-hot-list JSON (layer id -> top-C expert ids) ranked by combined
calibration prefill+decode selection frequency (token-weighted). Only the
calibration split is used: policy/final_validation never influence the
list (frozen contract).
"""
import argparse
import json
from array import array
from collections import Counter
from pathlib import Path
import struct
import sys

from analyze import frames

ROOT = Path(__file__).resolve().parents[2]
LAYERS = 48


def iter_groups(path):
    events = frames(path)
    next(events)  # header frame
    stage = None
    for payload in events:
        kind = struct.unpack('<I', payload[:4])[0]
        body = payload[12:]
        if kind == 2:
            _, stage, _, _ = struct.unpack('<QIQI', body)
        elif kind == 3:
            if stage is None:
                raise ValueError('routing frame before forward header')
            layer = struct.unpack('<I', body[:4])[0]
            ids = array('H')
            ids.frombytes(body[4:])
            if sys.byteorder != 'little':
                ids.byteswap()
            yield stage, layer, Counter(ids)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--plan', type=Path, required=True)
    ap.add_argument('--runs', type=Path, required=True)
    ap.add_argument('--capacity', type=int, required=True)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    out = args.output.resolve()
    if not any(out.is_relative_to(ROOT / p) for p in ['build', '.q4t-work']):
        ap.error('output must be under build/ or .q4t-work/')
    plan = json.loads(args.plan.read_text())
    entries = [e for e in plan['entries']
               if e['split'] == 'calibration' and e['mode'] != 'accept-tier']
    # Reuse entries: fixed ids. New runs: request order == entry order.
    mapping = {}
    for e in entries:
        if e['mode'] == 'reuse':
            mapping[e['id']] = (e['trace_dir'], e['trace_request'])
    for run in sorted(p.name for p in args.runs.iterdir()
                      if (args.runs / p / 'workload.json').is_file()):
        workload = json.loads((args.runs / run / 'workload.json').read_text())
        for i, eid in enumerate(workload['entries'], 1):
            mapping.setdefault(eid, (str(args.runs / run / 'trace'), i))
    missing = [e['id'] for e in entries if e['id'] not in mapping]
    if missing:
        raise RuntimeError(f'entries without traces: {missing}')

    combined = [Counter() for _ in range(LAYERS)]
    n_groups = 0
    for e in entries:
        tdir, rid = mapping[e['id']]
        for stage, layer, counts in iter_groups(
                Path(tdir) / f'request-{rid}.bin'):
            combined[layer].update(counts)
            n_groups += 1
    print(f'calibration: {len(entries)} requests, {n_groups} groups')
    hot = {}
    for layer in range(LAYERS):
        top = [e for e, _ in combined[layer].most_common(args.capacity)]
        hot[str(layer)] = top
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(hot))
    print(f'wrote {out} (C={args.capacity}, layers={LAYERS})')


if __name__ == '__main__':
    main()

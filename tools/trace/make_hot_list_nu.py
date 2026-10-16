"""Build per-layer static hot lists with NON-UNIFORM per-layer capacity.

Same frozen contract as make_hot_list.py: only the calibration split of the
business-set plan influences the lists (policy/final_validation never do),
ranked by combined calibration prefill+decode selection frequency
(token-weighted). The difference: each layer l gets exactly C_l experts,
where C_l comes from a per-layer capacity JSON ({layer: C}). The runtime
derives the layer's GPU slot count from the list length (capped by
--moe-resident-slots), so a list of C_l entries yields C_l slots.

Usage:
  make_hot_list_nu.py --plan PLAN --runs RUNS --capacity CAPJSON \
      --output OUT
  CAPJSON: {"0": 446, "1": 384, ...}  (one entry per layer, 0..47)
"""
import argparse
import json
from collections import Counter
from pathlib import Path

from make_hot_list import LAYERS, iter_groups

ROOT = Path(__file__).resolve().parents[2]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--plan', type=Path, required=True)
    ap.add_argument('--runs', type=Path, required=True)
    ap.add_argument('--capacity', type=Path, required=True,
                    help='JSON object mapping layer id -> per-layer C')
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    out = args.output.resolve()
    if not any(out.is_relative_to(ROOT / p) for p in ['build', '.q4t-work']):
        ap.error('output must be under build/ or .q4t-work/')
    plan = json.loads(args.plan.read_text())
    entries = [e for e in plan['entries']
               if e['split'] == 'calibration' and e['mode'] != 'accept-tier']
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

    cap = {int(k): int(v) for k, v in
           json.loads(args.capacity.read_text()).items()}
    if sorted(cap) != list(range(LAYERS)):
        raise RuntimeError(f'capacity must cover layers 0..{LAYERS-1}')

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
    total = 0
    for layer in range(LAYERS):
        c = cap[layer]
        if c < 1 or c > 512:
            raise RuntimeError(f'layer {layer}: capacity {c} out of [1,512]')
        top = [e for e, _ in combined[layer].most_common(c)]
        if len(top) < c:
            raise RuntimeError(f'layer {layer}: only {len(top)} distinct '
                               f'calibration experts, need {c}')
        hot[str(layer)] = top
        total += c
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(hot))
    print(f'wrote {out} (total slots={total}, '
          f'min={min(cap.values())}, max={max(cap.values())})')


if __name__ == '__main__':
    main()

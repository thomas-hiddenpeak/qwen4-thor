"""Per-layer per-stage statistics and capacity-miss curves (2026-09-30).

Consumes the frozen business set v1 traces (reuse + new runs) and reports,
per split (calibration / policy / final_validation) and stage
(prefill / decode):
  - per-layer expert selection frequencies;
  - miss curves over per-layer slot capacity for three policies:
      static  - fixed top-C list by calibration frequency;
      lru     - empty start, group-level LRU (oversized prefill groups
                stream misses without admission, matching replay.py);
      hybrid  - static top-C start, then LRU admission/eviction within C;
  - aggregate (uniform capacity) miss rates and resident bytes.

final_validation numbers are reported for validation only; they must not
be used to choose lists, capacities or policies.
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
LAYERS, EXPERTS, TOP_K = 48, 512, 10
SLOT_BYTES = 2765056  # 256-aligned expert payload (see replay.py)
CAPACITIES = [16, 24, 32, 48, 64, 96, 128, 192, 256, 384, 512]


def iter_groups(path):
    """Yield (stage, layer, Counter(expert->count)) in temporal order."""
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


class Lru:
    def __init__(self, capacity, seed=()):
        self.capacity = capacity
        self.resident = {}
        self.clock = 0
        for e in seed:
            self.resident[e] = self.clock

    def consume(self, need):
        # need: Counter expert->count for one group (temporal order).
        hits = [e for e in need if e in self.resident]
        missing = [e for e in need if e not in self.resident]
        route_hits = sum(need[e] for e in hits)
        self.clock += 1
        if len(need) <= self.capacity:
            victims = sorted((e for e in self.resident if e not in need),
                             key=lambda e: (self.resident[e], e))
            remove = max(0, len(self.resident) + len(missing) - self.capacity)
            for e in victims[:remove]:
                del self.resident[e]
            for e in need:
                self.resident[e] = self.clock
        return len(hits), route_hits, len(missing), int(not missing)


def load_groups(directory, entry_ids, request_ids):
    """Load groups for (trace_dir, request_id) pairs in temporal order."""
    out = []
    for entry, rid in zip(entry_ids, request_ids):
        path = Path(entry['trace_dir']) / f'request-{rid}.bin'
        out.append((entry, list(iter_groups(path))))
    return out


def pct(num: float, den: float) -> float:
    """Safe percent: 0.0 when the denominator is 0 (a layer/stage with no
    routed groups, e.g. requests without decode)."""
    return 100.0 * num / den if den else 0.0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--plan', type=Path, required=True)
    ap.add_argument('--runs', type=Path, required=True,
                    help='collection output dir (run_business_routing.py)')
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    out = args.output.resolve()
    if not any(out.is_relative_to(ROOT / p) for p in ['build', '.q4t-work']):
        ap.error('output must be under build/ or .q4t-work/')
    out.mkdir(parents=True, exist_ok=False)
    plan = json.loads(args.plan.read_text())
    entries = {e['id']: e for e in plan['entries']}

    # Map every entry to (trace_dir, request_id) in temporal order.
    # Reuse entries: fixed ids. New runs: request order == entry order.
    mapping = {}
    for e in plan['entries']:
        if e['mode'] == 'reuse':
            mapping[e['id']] = (e['trace_dir'], e['trace_request'])
    for run in sorted((args.runs / n).name for n in
                      (p.name for p in args.runs.iterdir())
                      if (args.runs / n / 'workload.json').is_file()):
        workload = json.loads((args.runs / run / 'workload.json').read_text())
        for i, eid in enumerate(workload['entries'], 1):
            mapping[eid] = (str(args.runs / run / 'trace'), i)
    # accept-tier entries are acceptance workload, not routing-analysis
    # material; they are not collected for this tool.
    skipped = [e['id'] for e in plan['entries'] if e['mode'] == 'accept-tier']
    missing = [e['id'] for e in plan['entries']
               if e['mode'] != 'accept-tier' and e['id'] not in mapping]
    if missing:
        raise RuntimeError(f'entries without traces: {missing}')
    if skipped:
        print(f'skipping accept-tier entries: {skipped}', flush=True)

    # Load all groups once, per entry, in plan order.
    print('loading traces...', flush=True)
    groups_by_entry = {}
    for e in plan['entries']:
        if e['mode'] == 'accept-tier':
            continue
        tdir, rid = mapping[e['id']]
        groups_by_entry[e['id']] = list(iter_groups(
            Path(tdir) / f'request-{rid}.bin'))
    print(f'loaded {sum(len(v) for v in groups_by_entry.values())} groups',
          flush=True)

    splits = ['calibration', 'policy', 'final_validation']
    result = {'schema': 1, 'slot_bytes': SLOT_BYTES,
              'capacities': CAPACITIES, 'splits': {}}
    for split in splits:
        ids = [e['id'] for e in plan['entries']
               if e['split'] == split and e['mode'] != 'accept-tier']
        # Per-layer frequencies per stage.
        freq = {stage: [Counter() for _ in range(LAYERS)]
                for stage in (1, 2)}
        # Per-layer group sequences per stage (temporal order preserved).
        seqs = {stage: [[] for _ in range(LAYERS)] for stage in (1, 2)}
        for eid in ids:
            for stage, layer, counts in groups_by_entry[eid]:
                freq[stage][layer].update(counts)
                seqs[stage][layer].append(counts)
        entry_result = {'requests': len(ids), 'frequencies': {},
                        'curves': {}}
        for stage, stage_name in ((1, 'prefill'), (2, 'decode')):
            top = {}
            for layer in range(LAYERS):
                ranked = freq[stage][layer].most_common(EXPERTS)
                top[layer] = [e for e, _ in ranked]
            entry_result['frequencies'][stage_name] = {
                str(layer): top[layer][:256] for layer in range(LAYERS)}
            curves = {}
            for capacity in CAPACITIES:
                static_rank = [top[l][:capacity] for l in range(LAYERS)]
                per_layer = {policy: [] for policy in
                             ('static', 'lru', 'hybrid')}
                for layer in range(LAYERS):
                    static_resident = set(static_rank[layer])
                    lru = Lru(capacity)
                    hybrid = Lru(capacity, seed=static_rank[layer])
                    # Token-based hit/miss (selection coverage) plus
                    # per-group load events (distinct experts loaded),
                    # matching replay.py: logical_bytes = loads x SLOT_BYTES.
                    s_hit = l_hit = h_hit = 0
                    s_route = l_route = h_route = 0
                    s_loads = l_loads = h_loads = 0
                    s_full = l_full = h_full = 0
                    n_groups = 0
                    for counts in seqs[stage][layer]:
                        n_groups += 1
                        need = set(counts)
                        routes = sum(counts.values())
                        sh = sum(counts[e] for e in need
                                 if e in static_resident)
                        s_hit += sh
                        s_route += routes
                        s_loads += len(need - static_resident)
                        s_full += int(need <= static_resident)
                        lh, lr, lm, lf = lru.consume(counts)
                        l_hit += lr
                        l_route += routes
                        l_loads += lm
                        l_full += lf
                        hh, hr, hm, hf = hybrid.consume(counts)
                        h_hit += hr
                        h_route += routes
                        h_loads += hm
                        h_full += hf
                    per_layer['static'].append(
                        dict(demands=s_route, hits=s_hit,
                             misses=s_route - s_hit, loads=s_loads,
                             groups=n_groups, full_hits=s_full,
                             miss_percent=pct(s_route - s_hit, s_route),
                             load_bytes=s_loads * SLOT_BYTES))
                    per_layer['lru'].append(
                        dict(demands=l_route, hits=l_hit,
                             misses=l_route - l_hit, loads=l_loads,
                             groups=n_groups, full_hits=l_full,
                             miss_percent=pct(l_route - l_hit, l_route),
                             load_bytes=l_loads * SLOT_BYTES))
                    per_layer['hybrid'].append(
                        dict(demands=h_route, hits=h_hit,
                             misses=h_route - h_hit, loads=h_loads,
                             groups=n_groups, full_hits=h_full,
                             miss_percent=pct(h_route - h_hit, h_route),
                             load_bytes=h_loads * SLOT_BYTES))
                curves[capacity] = {
                    'resident_bytes': capacity * LAYERS * SLOT_BYTES,
                    'per_layer': per_layer,
                    'aggregate': {}}
                for policy in ('static', 'lru', 'hybrid'):
                    rows = per_layer[policy]
                    demands = sum(r['demands'] for r in rows)
                    misses = sum(r['misses'] for r in rows)
                    curves[capacity]['aggregate'][policy] = dict(
                        demands=demands, misses=misses,
                        miss_percent=pct(misses, demands),
                        load_bytes=sum(r['load_bytes'] for r in rows))
            entry_result['curves'][stage_name] = curves
        result['splits'][split] = entry_result
        print(f'{split}: done', flush=True)
    (out / 'capacity_curve.json').write_text(
        json.dumps(result, ensure_ascii=False))
    # Compact report.
    lines = ['# Capacity-miss curves (business set v1)', '']
    for split in splits:
        lines.append(f'## {split} ({result["splits"][split]["requests"]} '
                     'requests)')
        for stage in ('prefill', 'decode'):
            lines.append(f'### {stage}')
            lines.append('| capacity | resident GB | static miss% '
                         '| lru miss% | hybrid miss% |')
            lines.append('|---:|---:|---:|---:|---:|')
            for capacity in CAPACITIES:
                c = result['splits'][split]['curves'][stage][capacity]
                a = c['aggregate']
                lines.append(
                    f"| {capacity} | {c['resident_bytes'] / 1e9:.2f} | "
                    f"{a['static']['miss_percent']:.2f} | "
                    f"{a['lru']['miss_percent']:.2f} | "
                    f"{a['hybrid']['miss_percent']:.2f} |")
            lines.append('')
    (out / 'report.md').write_text('\n'.join(lines))
    print('wrote', out / 'capacity_curve.json', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

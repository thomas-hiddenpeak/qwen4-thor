#!/usr/bin/env python3
"""Backfill the memory ledger with real round-3 [q4t][budget] values.

Parses, per e2e tag:
  * server.log: [q4t][budget] lines (weights/fixed/per_request/state_pool/
    max_len/max_seq/capped) and the [q4t][residency] non-uniform per-layer C
    line (total + per-layer C_l list);
  * memory/memory-peak.json: service_total_physical_peak_bytes (written when
    the monitor exits, i.e. after the whole config finishes);
  * memory/memory.csv: max of svc_used delta + gpu_bytes as a cross-check.

Prints a markdown table row per tag plus PASS/FAIL vs the ledger expectations
(docs/MOE_RESIDENCY_MEMORY_LEDGER_2026-10-01.md section 1.4).

Usage: python3 budget_backfill.py r3-cand-c256 r3-cand-nu15552
"""
import csv
import json
import os
import re
import sys

W = os.path.dirname(os.path.abspath(__file__))

# Ledger expectations (decimal GB, 2026-10-01 ledger section 1.4):
#   weights = full 84.00 GB - (24576 - resident_slots) * 2,764,816 B
#   fixed   = weights + 10.15 GB (ws+buf+margin) + L2 pinned
#   L2-8    = 48 * 8 * 3,276,816 B = 1.26 GB
SLOT = 2_764_816
STAGE = 3_276_816
FULL_W = 84.00e9
NONW_FIXED = 10.15e9
EXPECTED = {
    'r3-cand-c256': dict(resident=12288, l2=8,
                         weights_gb=50.02, fixed_gb=61.43),
    'r3-cand-nu15552': dict(resident=15552, l2=8,
                            weights_gb=59.05, fixed_gb=70.46),
}


def parse_budget(log_path):
    out = {}
    nu = None
    if not os.path.exists(log_path):
        return out, nu
    for line in open(log_path, errors='replace'):
        if '[q4t][budget]' not in line:
            continue
        for k in ('mem_total', 'budget', 'weights', 'fixed',
                  'per_request', 'state_pool'):
            m = re.search(rf'{k}=([\d.]+) GB', line)
            if m:
                out[k] = float(m.group(1))
        m = re.search(r'max_len=(\d+) max_seq=(\d+)(\s+\(CAPPED.*)?', line)
        if m:
            out['max_len'] = int(m.group(1))
            out['max_seq'] = int(m.group(2))
            out['capped'] = bool(m.group(3))
        m = re.search(
            r'\[q4t\]\[residency\] non-uniform per-layer C: total=(\d+) '
            r'\(max/layer=(\d+)\): ([\d,]+)', line)
        if m:
            nu = dict(total=int(m.group(1)), cap=int(m.group(2)),
                      c_layer=[int(x) for x in m.group(3).split(',')])
    return out, nu


def mem_peak(tag):
    d = os.path.join(W, f'e2e-{tag}', 'memory')
    peak = None
    p = os.path.join(d, 'memory-peak.json')
    if os.path.exists(p):
        try:
            peak = json.load(open(p)).get(
                'service_total_physical_peak_bytes')
        except (json.JSONDecodeError, OSError):
            peak = None
    # Cross-check: recompute sim_peak = max(rss_kb*1024 + gpu_bytes) from
    # the csv (same definition as service_total_physical_peak_bytes).
    cross = None
    c = os.path.join(d, 'memory.csv')
    if os.path.exists(c):
        with open(c) as f:
            rows = list(csv.DictReader(f))
        vals = [int(r['rss_kb']) * 1024 + int(r['gpu_bytes'] or 0)
                for r in rows if int(r['gpu_bytes'] or 0) >= 0]
        if vals:
            cross = max(vals)
    return peak, cross


def main():
    tags = sys.argv[1:] or list(EXPECTED)
    print('| tag | budget weights (GB) | budget fixed (GB) | '
          'max_len/max_seq | per-layer C total | mem peak (GB) | '
          'csv cross-check (GB) | vs ledger |')
    print('|---|---|---|---|---|---|---|---|')
    for tag in tags:
        b, nu = parse_budget(os.path.join(W, f'e2e-{tag}', 'server.log'))
        peak, cross = mem_peak(tag)
        exp = EXPECTED.get(tag)
        ok = 'n/a'
        if exp and b:
            ok = 'PASS' if (
                abs(b.get('weights', -1) * 1e9
                    - (FULL_W - (24576 - exp['resident']) * SLOT)) < 0.5e9
                and abs(b.get('fixed', -1) * 1e9
                        - (FULL_W - (24576 - exp['resident']) * SLOT
                           + NONW_FIXED + exp['l2'] * 48 * STAGE)) < 0.75e9
                and b.get('max_len') == 262144 and b.get('max_seq') == 1
                and not b.get('capped')) else 'FAIL'
        nu_s = (f"{nu['total']} (max {nu['cap']}, layers "
                f"{len(nu['c_layer'])})" if nu else
                (f"uniform (total {b.get('weights', '?')} GB line)"
                 if b else 'missing'))
        print(f"| {tag} | {b.get('weights', '?')} | {b.get('fixed', '?')} | "
              f"{b.get('max_len', '?')}/{b.get('max_seq', '?')}"
              f"{' (CAPPED)' if b.get('capped') else ''} | {nu_s} | "
              f"{peak / 1e9:.2f}" if peak else
              f"| {tag} | {b.get('weights', '?')} | {b.get('fixed', '?')} | "
              f"{b.get('max_len', '?')}/{b.get('max_seq', '?')} | {nu_s} | "
              f"{'?' if peak is None else '?'} | "
              f"{cross / 1e9:.2f}" if cross else
              f"| {tag} | {b.get('weights', '?')} | {b.get('fixed', '?')} | "
              f"{b.get('max_len', '?')}/{b.get('max_seq', '?')} | {nu_s} | "
              f"pending | pending | {ok} |")
        if nu and exp:
            if nu['total'] != exp['resident']:
                print(f"  !! {tag}: per-layer C total {nu['total']} != "
                      f"expected {exp['resident']}")
    print()
    print('Note: mem peak is written by monitor_memory.py only after the')
    print('whole config (all tiers) finishes; rerun this script then.')


if __name__ == '__main__':
    main()

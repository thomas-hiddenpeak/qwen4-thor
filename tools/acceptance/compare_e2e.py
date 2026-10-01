#!/usr/bin/env python3
"""Frozen-criteria comparison: baseline (C=0) vs candidate (C=256+hot-256).

Criteria (frozen 2026-09-30):
  * decode throughput per tier = (out-1)/(latency-ttft) per request;
    gate: candidate harmonic mean >= 50% of baseline harmonic mean, per tier;
    first request and subsequent requests listed separately.
  * TTFT arithmetic mean; total latency arithmetic mean (reported).
  * load volume from server.log [residency] lines (loads/load_mb/misses,
    decode vs prefill split).
  * memory peak: service_total_physical_peak_bytes from memory-peak.json.
  * correctness: per-tier output sha256 bit-exact baseline vs candidate;
    target tier must have in=261887 out=257 finish=length (total 262144).
"""
import json
import re
from pathlib import Path

W = Path(__file__).resolve().parent
TIERS = [1024, 4096, 8192, 45056, 204800, 261887]
TARGET = 261887


def harmonic(xs):
    xs = [x for x in xs if x is not None and x > 0]
    return len(xs) / sum(1.0 / x for x in xs) if xs else float('nan')


def mean(xs):
    return sum(xs) / len(xs) if xs else float('nan')


def load_results(tag):
    p = W / f'e2e-{tag}' / 'results.json'
    if not p.is_file():
        return {}
    return {it['length']: it for it in json.loads(p.read_text())}


def latencies(tag, tier):
    p = W / f'e2e-{tag}' / f'context-{tier}' / 'responses.json'
    if not p.is_file():
        return []
    return [r['latency'] for r in json.loads(p.read_text())]


def residency_lines(tag):
    log = W / f'e2e-{tag}' / 'server.log'
    if not log.is_file():
        return []
    out = []
    for line in log.read_text(errors='replace').splitlines():
        m = re.match(
            r'\[q4t\]\[residency\] id=(\S+) finish=(\S+) in=(\d+) out=(\d+)'
            r' loads=(\d+) load_mb=([\d.]+) misses=(\d+) hits=(\d+)'
            r' evictions=(\d+)(?: dmiss=(\d+) dlook=(\d+) pmiss=(\d+)'
            r' plook=(\d+))?(?: l2h=(\d+) l2m=(\d+) l2ev=(\d+)'
            r' nvme_mb=([\d.]+))?', line)
        if m:
            out.append(dict(zip(
                ['id', 'finish', 'in', 'out', 'loads', 'load_mb', 'misses',
                 'hits', 'evictions', 'dmiss', 'dlook', 'pmiss', 'plook',
                 'l2h', 'l2m', 'l2ev', 'nvme_mb'],
                m.groups())))
    return out


def mem_peak(tag):
    p = W / f'e2e-{tag}' / 'memory' / 'memory-peak.json'
    if not p.is_file():
        return None
    d = json.loads(p.read_text())
    return d.get('service_total_physical_peak_bytes')


def main():
    import sys
    args = sys.argv[1:]
    base_tag = args[0] if len(args) > 0 else 'baseline-s0-current'
    cand_tag = args[1] if len(args) > 1 else 'cand-c256'
    base = load_results(base_tag)
    cand = load_results(cand_tag)
    res_b = residency_lines(base_tag)
    res_c = residency_lines(cand_tag)
    print('== E2E matrix comparison (frozen criteria, 2026-09-30) ==')
    print(f"{'tier':>8} | {'base dec tps (hmean)':>22} | {'cand dec tps (hmean)':>22} | "
          f"{'ratio':>6} | {'gate50%':>7} | base TTFT(s) | cand TTFT(s) | "
          f"base lat(s) | cand lat(s) | bitexact")
    all_pass = True
    pending = []
    for t in TIERS:
        if t not in base or t not in cand:
            pending.append(t)
            print(f"{t:>8} | {'PENDING (tier data incomplete)':>22} | "
                  f"{'':>22} | {'':>6} | {'PENDING':>7} | - | - | - | - | -")
            continue
        b, c = base[t], cand[t]
        bd = [m['decode_tps'] for m in b['metrics']]
        cd = [m['decode_tps'] for m in c['metrics']]
        bh, ch = harmonic(bd), harmonic(cd)
        ratio = ch / bh if bh and bh > 0 else float('nan')
        ok = ratio >= 0.5
        all_pass &= ok
        bt = [m['ttft'] for m in b['metrics']]
        ct = [m['ttft'] for m in c['metrics']]
        bl = latencies(base_tag, t)
        cl = latencies(cand_tag, t)
        exact = b['outputs'] == c['outputs']
        print(f"{t:>8} | {bh:>22.4f} | {ch:>22.4f} | {ratio:>6.3f} | "
              f"{'PASS' if ok else 'FAIL':>7} | {mean(bt):>12.2f} | "
              f"{mean(ct):>12.2f} | "
              f"{mean(bl):>11.2f} | {mean(cl):>11.2f} | "
              f"{'yes' if exact else 'NO':>8}")
        # per-request detail (first vs subsequent)
        for i in range(min(3, len(b['metrics']), len(c['metrics']))):
            bl = b['metrics'][i]
            cl = c['metrics'][i]
            print(f"         run{i+1}: base ttft={bl['ttft']:.2f}s dec={bd[i]:.2f} "
                  f"| cand ttft={cl['ttft']:.2f}s dec={cd[i]:.2f}")
    # target tier token contract
    print('\n== target tier token contract (total context 262144) ==')
    for tag, ttag in [('base', base_tag), ('cand', cand_tag)]:
        rp = W / f'e2e-{ttag}' / f'context-{TARGET}' / 'responses.json'
        if not rp.is_file():
            print(f"{tag}: PENDING (responses.json missing)")
            continue
        rows = json.loads(rp.read_text())
        ok = all(r['actual_input'] == 261887 and r['actual_output'] == 257
                 and r['finish'] == ['length'] for r in rows)
        print(f"{tag}: in/out/finish per request = "
              f"{[(r['actual_input'], r['actual_output'], r['finish']) for r in rows]} "
              f"-> {'OK' if ok else 'FAIL'}")
    # load volume
    print('\n== residency load volume (candidate) ==')
    for r in res_c:
        extra = ''
        if r.get('dmiss') is not None:
            extra = (f" dmiss={r['dmiss']}/{r['dlook']} pmiss={r['pmiss']}"
                     f"/{r['plook']}")
        if r.get('l2h') is not None:
            l2h, l2m = int(r['l2h']), int(r['l2m'])
            hit_rate = 100.0 * l2h / (l2h + l2m) if (l2h + l2m) else 0.0
            extra += (f" l2h={l2h} l2m={l2m} l2ev={r['l2ev']}"
                      f" l2hit%={hit_rate:.1f} nvme_mb={r['nvme_mb']}")
        print(f"  {r['id']} in={r['in']} out={r['out']} loads={r['loads']} "
              f"load_mb={r['load_mb']} misses={r['misses']} "
              f"evictions={r['evictions']}{extra}")
    print(f"baseline residency lines: {len(res_b)} (expected 0 for C=0)")
    # memory
    print('\n== memory peak (service_total_physical_peak_bytes) ==')
    for tag in [base_tag, cand_tag]:
        v = mem_peak(tag)
        print(f"  {tag}: {v}" + ('' if v is None else
              f"  ({v/1e9:.3f} GB, budget 54.000 GB"
              f"{', user-accepted overrun for C=256' if tag==cand_tag else ''})"))
    print('\n== GATE: decode >= 50% of baseline per tier ==')
    if pending:
        print(f"INCOMPLETE: pending tiers {pending} (gate not evaluable yet)")
    else:
        print('ALL PASS' if all_pass else 'FAIL (see tiers above)')


if __name__ == '__main__':
    main()

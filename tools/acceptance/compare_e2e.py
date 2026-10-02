#!/usr/bin/env python3
"""Frozen-criteria comparison: baseline (C=0) vs candidate (C=256+hot-256).

Criteria (frozen 2026-09-30):
  * decode throughput per tier = (out-1)/(latency-ttft) per request;
    gate: candidate harmonic mean >= 50% of baseline harmonic mean, per tier;
    first request and subsequent requests listed separately.
  * TTFT arithmetic mean; total latency arithmetic mean (reported).
  * load volume from server.log [residency] lines (loads/load_mb/misses,
    decode vs prefill split).
  * memory counters are diagnostic; the independent memory gate is required.
  * correctness: per-tier HTTP text sha256 baseline vs candidate;
    target tier must have in=261887 out=257 finish=length (total 262144).
"""
import argparse
import hashlib
import json
import math
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
W = ROOT / '.q4t-work/moe-residency-20260930'
TIERS = [1024, 4096, 8192, 45056, 204800, 261887]
TARGET = 261887


def harmonic(xs):
    return len(xs) / sum(1.0 / x for x in xs) if xs else float('nan')


def mean(xs):
    return sum(xs) / len(xs) if xs else float('nan')


def load_results(tag):
    p = W / f'e2e-{tag}' / 'results.json'
    if not p.is_file():
        return {}
    rows = json.loads(p.read_text())
    if not isinstance(rows, list):
        raise ValueError(f'{p}: expected result list')
    result = {it['length']: it for it in rows}
    if len(result) != len(rows):
        raise ValueError(f'{p}: duplicate tiers')
    return result


def validate_exit(tag):
    """Require a successful runner AND server shutdown before accepting rows."""
    path = W / f'e2e-{tag}' / 'exit.json'
    raw = path.read_text()
    try:
        status = json.loads(raw)
    except json.JSONDecodeError:
        # Frozen legacy wrapper replaced exit.json with these key=value lines.
        # Its runner exit code includes run_acceptance's server-exit check.
        pairs = dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)
        if pairs.get('runner_rc') != '0' or pairs.get('tag') != tag:
            raise ValueError(f'{tag}: legacy runner exit did not pass')
        return
    if (status.get('server') != 0 or
            status.get('http_output_checks_passed') is not True or
            status.get('failure') is not None or status.get('completed') != len(TIERS)):
        raise ValueError(f'{tag}: runner/server completion contract failed')
    wrapper = path.with_name('wrapper-exit.json')
    if wrapper.is_file():
        status = json.loads(wrapper.read_text())
        if status.get('runner_rc') != 0 or status.get('monitor_rc') != 0:
            raise ValueError(f'{tag}: wrapper runner/monitor exit failed')


def validate_tier(tag, tier, item):
    """Validate raw HTTP evidence before trusting cached summary metrics."""
    path = W / f'e2e-{tag}' / f'context-{tier}' / 'responses.json'
    rows = json.loads(path.read_text())
    metrics, outputs = item['metrics'], item['outputs']
    if not isinstance(rows, list) or len(rows) < 3:
        raise ValueError('at least three HTTP responses are required')
    if len(metrics) != len(rows) or len(outputs) != len(rows):
        raise ValueError('response/metric/output counts differ')
    prompt_hash = item['prompt_sha256']
    if not isinstance(prompt_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', prompt_hash):
        raise ValueError('missing or invalid prompt identity')
    expected_output = 257 if tier == TARGET else 256
    for i, (row, metric, digest) in enumerate(zip(rows, metrics, outputs), 1):
        if (row['success'] != 1 or row['actual_input'] != tier or
                row['actual_output'] != expected_output or
                row['finish'] != ['length']):
            raise ValueError(f'run{i}: HTTP/token/finish contract failed')
        if row['prompt_sha256'] != prompt_hash:
            raise ValueError(f'run{i}: prompt identity differs')
        if hashlib.sha256(row['text'].encode()).hexdigest() != digest:
            raise ValueError(f'run{i}: output digest differs from saved text')
        values = (metric['ttft'], metric['decode_tps'], row['ttft'], row['latency'])
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or
               not math.isfinite(v) or v <= 0 for v in values):
            raise ValueError(f'run{i}: non-finite/non-positive metrics')
        if row['latency'] <= row['ttft']:
            raise ValueError(f'run{i}: latency must exceed TTFT')
        rate = (expected_output - 1) / (row['latency'] - row['ttft'])
        if (not math.isclose(metric['ttft'], row['ttft'], rel_tol=1e-9) or
                not math.isclose(metric['decode_tps'], rate, rel_tol=1e-9)):
            raise ValueError(f'run{i}: summary metrics differ from HTTP evidence')
    if item.get('deterministic') is not True or len(set(outputs)) != 1:
        raise ValueError('repeated output is not deterministic')


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
            r' nvme_mb=([\d.]+))?(?: mh=(\d+) mw=(\d+) msk=(\d+)'
            r' ld2h=(\d+) ld2m=(\d+) ld2ev=(\d+) lp2h=(\d+) lp2m=(\d+)'
            r' lp2ev=(\d+))?', line)
        if m:
            out.append(dict(zip(
                ['id', 'finish', 'in', 'out', 'loads', 'load_mb', 'misses',
                 'hits', 'evictions', 'dmiss', 'dlook', 'pmiss', 'plook',
                 'l2h', 'l2m', 'l2ev', 'nvme_mb', 'mh', 'mw', 'msk',
                 'ld2h', 'ld2m', 'ld2ev', 'lp2h', 'lp2m', 'lp2ev'],
                m.groups())))
    return out


def mem_peak(tag):
    p = W / f'e2e-{tag}' / 'memory' / 'memory-peak.json'
    if not p.is_file():
        return None
    d = json.loads(p.read_text())
    return d.get('service_total_physical_peak_bytes')


def main():
    global W
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('base_tag', nargs='?', default='baseline-s0-current')
    parser.add_argument('cand_tag', nargs='?', default='cand-c256')
    parser.add_argument('--work-dir', type=Path,
                        default=Path(os.environ.get('Q4T_ACCEPTANCE_WORK_DIR', W)))
    parser.add_argument('--minimum-ratio', type=float, default=0.5)
    args = parser.parse_args()
    if not math.isfinite(args.minimum_ratio) or args.minimum_ratio <= 0:
        parser.error('--minimum-ratio must be finite and positive')
    W = args.work_dir.resolve()
    base_tag, cand_tag = args.base_tag, args.cand_tag
    validate_exit(base_tag)
    validate_exit(cand_tag)
    base = load_results(base_tag)
    cand = load_results(cand_tag)
    res_b = residency_lines(base_tag)
    res_c = residency_lines(cand_tag)
    print('== E2E matrix comparison (frozen criteria, 2026-09-30) ==')
    print(f"{'tier':>8} | {'base dec tps (hmean)':>22} | {'cand dec tps (hmean)':>22} | "
          f"{'ratio':>6} | {'gate':>7} | base TTFT(s) | cand TTFT(s) | "
          f"base lat(s) | cand lat(s) | text-exact")
    all_pass = True
    pending = []
    for t in TIERS:
        if t not in base or t not in cand:
            pending.append(t)
            print(f"{t:>8} | {'PENDING (tier data incomplete)':>22} | "
                  f"{'':>22} | {'':>6} | {'PENDING':>7} | - | - | - | - | -")
            continue
        b, c = base[t], cand[t]
        try:
            validate_tier(base_tag, t, b)
            validate_tier(cand_tag, t, c)
            if b['prompt_sha256'] != c['prompt_sha256']:
                raise ValueError('baseline/candidate prompts differ')
        except (OSError, ValueError, KeyError, TypeError) as error:
            all_pass = False
            print(f'{t:>8} | INVALID: {error}')
            continue
        bd = [m['decode_tps'] for m in b['metrics']]
        cd = [m['decode_tps'] for m in c['metrics']]
        bh, ch = harmonic(bd), harmonic(cd)
        ratio = ch / bh if bh and bh > 0 else float('nan')
        ok = ratio >= args.minimum_ratio
        exact = b['outputs'] == c['outputs']
        all_pass &= ok and exact
        bt = [m['ttft'] for m in b['metrics']]
        ct = [m['ttft'] for m in c['metrics']]
        bl = latencies(base_tag, t)
        cl = latencies(cand_tag, t)
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
    print('\nTarget token contract: validated with every tier above '
          '(261887 input + 257 output = 262144).')
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
        if r.get('mh') is not None:
            ld2h, ld2m = int(r['ld2h']), int(r['ld2m'])
            lp2h, lp2m = int(r['lp2h']), int(r['lp2m'])
            dh = 100.0 * ld2h / (ld2h + ld2m) if (ld2h + ld2m) else 0.0
            ph = 100.0 * lp2h / (lp2h + lp2m) if (lp2h + lp2m) else 0.0
            extra += (f" mh={r['mh']} mw={r['mw']} msk={r['msk']}"
                      f" ld2h={ld2h} ld2m={ld2m} ld2ev={r['ld2ev']}"
                      f" ld2hit%={dh:.1f} lp2h={lp2h} lp2m={lp2m}"
                      f" lp2ev={r['lp2ev']} lp2hit%={ph:.1f}")
        print(f"  {r['id']} in={r['in']} out={r['out']} loads={r['loads']} "
              f"load_mb={r['load_mb']} misses={r['misses']} "
              f"evictions={r['evictions']}{extra}")
    print(f"baseline residency lines: {len(res_b)} (expected 0 for C=0)")
    # memory
    print('\n== legacy RSS+driver diagnostic (not a physical-memory gate) ==')
    for tag in [base_tag, cand_tag]:
        v = mem_peak(tag)
        print(f"  {tag}: {v}" + ('' if v is None else f"  ({v/1e9:.3f} GB)"))
    print(f'\n== GATE: valid HTTP evidence, identical text, decode >= '
          f'{100 * args.minimum_ratio:g}% of baseline per tier ==')
    if pending:
        print(f"INCOMPLETE: pending tiers {pending} (gate not evaluable yet)")
    else:
        print('ALL PASS' if all_pass else 'FAIL (see tiers above)')
    return 0 if all_pass and not pending else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, TypeError, ZeroDivisionError) as error:
        print(f'INVALID EVIDENCE: {error}', file=sys.stderr)
        sys.exit(1)

"""Frozen 45K off/on screening; never grants full performance acceptance.

This reads completed evidence only. Three observations describe their observed
range, not a confidence interval or a statistical noninferiority guarantee.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import statistics

ROOT = Path(__file__).resolve().parents[2]
CAPACITY = {'max_len': 262144, 'max_seq': 1, 'max_prefill': 8192}


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def screen_metrics(baseline, candidate):
    require(len(baseline) == len(candidate) == 3, 'exactly three observations required')
    for row in baseline + candidate:
        require(all(type(row[k]) in (float, int) and math.isfinite(row[k]) and
                    row[k] > 0 for k in ('ttft', 'decode_tps')), 'invalid metric')
    btt, ctt = ([r['ttft'] for r in rows] for rows in (baseline, candidate))
    bdec, cdec = ([r['decode_tps'] for r in rows] for rows in (baseline, candidate))
    checks = {'cold_ttft_strictly_improves': ctt[0] < btt[0],
              'both_warm_ttft_below_baseline_warm_min': max(ctt[1:]) < min(btt[1:]),
              'every_decode_at_least_baseline_observed_min': min(cdec) >= min(bdec)}
    def stats(ttft, decode):
        return {'cold_ttft_s': ttft[0], 'warm_ttft_s': ttft[1:],
                'ttft_mean_s': statistics.mean(ttft),
                'warm_ttft_mean_s': statistics.mean(ttft[1:]),
                'ttft_range_s': [min(ttft), max(ttft)],
                'warm_ttft_range_s': [min(ttft[1:]), max(ttft[1:])],
                'decode_tps': decode,
                'decode_harmonic_mean_tps': statistics.harmonic_mean(decode),
                'decode_range_tps': [min(decode), max(decode)]}
    passed = all(checks.values())
    return {'decision': 'PASS_SCREENING' if passed else 'NO_GO',
            'screening_passed': passed, 'checks': checks,
            'baseline': stats(btt, bdec), 'candidate': stats(ctt, cdec),
            'warm_ttft_ranges_overlap': max(min(btt[1:]), min(ctt[1:])) <=
                                        min(max(btt[1:]), max(ctt[1:])),
            'performance_acceptance': False,
            'rule': 'one frozen off->on pair; no adaptive extra repeats',
            'inference_limit': 'observed ranges only; no statistical confidence claim'}


def compare_evidence(baseline, candidate, quality):
    sources = {}
    def read(directory, relative):
        path = directory / relative
        raw = path.read_bytes()
        sources[str(path)] = hashlib.sha256(raw).hexdigest()
        return json.loads(raw)

    def completed(directory, mode, order):
        p = read(directory, 'protocol.json')
        x = read(directory, 'wrapper-exit.json')
        e = read(directory, 'http/exit.json')
        capacity = read(directory, 'http/capacity.json')
        command = read(directory, 'http/server-command.json')
        require(p['mode'] == mode and p['chunk_order'] == order, 'wrong run mode/order')
        require(x['runner_rc'] == x['monitor_rc'] == 0 and not x['failure'] and
                not x['cleanup_failed'] and
                x['unit_after_cleanup']['LoadState'] == 'not-found', 'wrapper failed/unclean')
        require(e['server'] == 0 and e['http_output_checks_passed'] is True and
                not e['failure'] and not e['cleanup_failure'], 'HTTP evidence failed')
        require(capacity['matches_requested'] is True and
                capacity['requested'] == capacity['effective'] == CAPACITY,
                'capacity mismatch')
        require(command['effective_q4t_environment'] == p['effective_environment'] and
                p['effective_environment']['Q4T_MOE_CHUNK_ORDER'] == str(order),
                'effective chunk-order environment mismatch')
        require(command['isolation']['host_cache_max_bytes'] == p['host_cache_max_bytes']
                and command['isolation']['swap_max_bytes'] == p['swap_max_bytes'] == 0,
                'isolation mismatch')
        binary_sha = (directory / 'http/binary.sha256').read_text().strip()
        require(binary_sha == p['binary_sha256'], 'binary identity mismatch')
        return p, x, e

    q, qexit, qe = completed(quality, 'quality', 1)
    qr = read(quality, 'http/results.json')
    require(len(qr) == 11 and qe['completed'] == 11 and
            len({r['id'] for r in qr}) == 11 and all(
                r['success'] == 1 and r['exact_match'] is True and
                r['length_match'] is True and r['finish'] == ['stop'] and
                r['requested_capacity'] == r['effective_capacity'] == CAPACITY
                for r in qr), 'fixed quality 11 contract failed')
    runs = []
    for directory, order in [(baseline, 0), (candidate, 1)]:
        p, x, e = completed(directory, 'performance', order)
        require(p['lengths'] == [45056] and p['repeats'] == 3 and
                p['host_cache_max_bytes'] == 16 << 30 and
                p['performance_plan']['scope'] == 'partial', 'not the frozen bounded run')
        require(p['monitor']['interval_seconds'] == 1 and
                p['monitor']['gpu_interval_seconds'] == 10 and
                p['monitor']['file_cache_mode'] == 'endpoints', 'wrong monitoring protocol')
        gate = read(directory, 'cache-gate.json')
        require(gate['cold_payload_established'] is True and
                gate['payload_resident_bytes'] == 0 and not gate['advice_errors'],
                'cold cache not established')
        results = read(directory, 'http/results.json')
        rows = read(directory, 'http/context-45056/responses.json')
        require(len(results) == 1 and results[0]['length'] == 45056 and len(rows) == 3,
                'missing/extra performance requests')
        require(e['completed'] == 1 and e['partial_performance_matrix'] is True and
                e['full_offload_matrix_completed'] is False, 'partial scope misreported')
        for r in rows:
            require(r['success'] == 1 and r['actual_input'] == 45056 and
                    r['actual_output'] == r['requested_max_tokens'] == 256 and
                    r['finish'] == ['length'] and
                    r['requested_capacity'] == r['effective_capacity'] == CAPACITY,
                    'request output/capacity contract failed')
            require(math.isfinite(r['latency']) and math.isfinite(r['ttft']) and
                    r['latency'] > r['ttft'] > 0, 'invalid raw request metrics')
        digests = [hashlib.sha256(r['text'].encode()).hexdigest() for r in rows]
        require(len(set(digests)) == 1 and digests == results[0]['outputs'] and
                len({r['prompt_sha256'] for r in rows}) == 1 and
                rows[0]['prompt_sha256'] == results[0]['prompt_sha256'],
                'prompt/output identity mismatch')
        metrics = [{'ttft': r['ttft'], 'decode_tps': 255 / (r['latency'] - r['ttft'])}
                   for r in rows]
        require(metrics == results[0]['metrics'], 'summary differs from raw request metrics')
        runs.append((p, x, rows, metrics))
    bp, bx, br, bm = runs[0]
    cp, cx, cr, cm = runs[1]
    for key in ('binary_sha256', 'tool_sha256', 'fixture_sha256', 'model_files',
                'monitor', 'lengths', 'repeats', 'host_cache_max_bytes', 'swap_max_bytes',
                'max_len', 'max_seq', 'max_prefill', 'request_deadline_ms', 'client_lifecycle'):
        require(bp[key] == cp[key], 'unfair pair: ' + key)
    require(bp['binary_sha256'] == q['binary_sha256'], 'quality used another binary')
    require(qexit['ended_t'] <= bx['started_t'] and bx['ended_t'] <= cx['started_t'],
            'frozen quality -> off -> on ordering not established')
    require({k:v for k,v in bp['effective_environment'].items() if k!='Q4T_MOE_CHUNK_ORDER'} ==
            {k:v for k,v in cp['effective_environment'].items() if k!='Q4T_MOE_CHUNK_ORDER'},
            'environment differs beyond chunk-order')
    require(all(a['text'] == b['text'] and a['prompt_sha256'] == b['prompt_sha256']
                for a,b in zip(br,cr)), 'candidate output/prompt differs from baseline')
    return {'schema': 1, **screen_metrics(bm,cm), 'source_sha256': sources,
            'evidence_contracts_passed': True, 'quality_11_passed': True,
            'scope': 'single 45056 tier, three requests per condition',
            'total_physical_RAM': 'INDETERMINATE',
            'file_cache_during_requests': 'NOT_SAMPLED'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('baseline', 'candidate', 'quality', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    args = parser.parse_args()
    out = args.output.resolve()
    if not any(out.is_relative_to(ROOT / d) for d in ('build', '.q4t-work')):
        parser.error('output must be under build/ or .q4t-work/')
    if out.exists():
        parser.error('output already exists; earlier evidence is immutable')
    try:
        result = compare_evidence(args.baseline.resolve(), args.candidate.resolve(),
                                  args.quality.resolve())
        rc = 0 if result['screening_passed'] else 1
    except (ValueError, KeyError, OSError, TypeError, IndexError) as error:
        result = {'schema': 1, 'decision': 'INVALID_EVIDENCE',
                  'failure': type(error).__name__ + ': ' + str(error),
                  'performance_acceptance': False, 'screening_passed': False}
        rc = 2
    with out.open('x') as stream:
        json.dump(result, stream, indent=2)
        stream.write('\n')
    print(result['decision'])
    return rc


if __name__ == '__main__':
    raise SystemExit(main())

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

from offload_policy import (AXES, bound_file, check_numerical,
    check_partition_plan, check_policy_protocol, partition_path_evidence,
    read_bound_json)

ROOT = Path(__file__).resolve().parents[2]
CAPACITY = {'max_len': 262144, 'max_seq': 1, 'max_prefill': 8192}
# This comparator is intentionally specific to the frozen 2026-10-03 screen.
# Source identities were checked against plan.json before its first HTTP run.
MODEL = ROOT.parent / 'llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream'
HOT = ROOT / '.q4t-work/moe-residency-20260930/hot-lists/hot-final-12288.json'
QUALITY_FIXTURES = ROOT / '.q4t-work/moe-trace-runtime-20260928/quality-off/inputs'
QUALITY_REFERENCE = ROOT / '.q4t-work/offload-budget-goal-20261002/quality/http/results.json'
PERF_FIXTURES = ROOT / '.q4t-work/moe-residency-20260930/e2e-fixtures-v2'
PERF_REFERENCE = ROOT / '.q4t-work/moe-residency-20260930/e2e-acc-base-c0/results.json'
PERF_REFERENCE_SHA256 = 'c1551a5654d3d608e6149da3d73c003a2b67843179fbc0ad7e41ea444ec3e325'
PERF_FIXTURE_SHA256 = '7e993e1bba5512f3d6af11af4472b1d032b68bc38475b6730e8fc9f3ae31f83b'
FROZEN_CONFIG_SHA256 = {
    str(HOT): '863e2ad6d3ccd3afeb7b67254ffd30a1dd15a020a44ed50ff886b37dce3ccb99',
    str(MODEL / 'config.json'): 'e765305daba0951974308f4d32c075b52a6a45974730d273f2216718a994d624',
    str(MODEL / 'model.safetensors.index.json'): 'aa702aa8cadb463b728cfd6829e172b1124114080231c75712155e2490f8c607',
}
FROZEN_QUALITY_SHA256 = {
    str(QUALITY_FIXTURES / 'manifest.json'): 'e2a542bf2c27b4404844b2e60e861c80e6c670d32bcd78685ad053f7fb833ae1',
    str(QUALITY_FIXTURES / 'requests.jsonl'): '22cce46de658df1988cacbe311bc74a7d6f1e7125c48e11e1cd9a0dd97ab4c42',
    str(QUALITY_REFERENCE): '953d677873fee16260379a756fb70e3008eab3dcc2f88306eb20f7a50c88095c',
}
# id, input tokens, prompt SHA, exact unstripped reference text, output tokens.
QUALITY_CASES = (
    ('1024-10', 1024, '9f0c45cf798569d3b2af33bd7847fdfbd5254c4e37a9d774a2fbd22b42516392', '710003', 7),
    ('1024-50', 1024, 'f17889bf3cec9aead6db4da85c4d517a4d795407932c30b9f273363dec239dd0', '710130', 7),
    ('1024-90', 1024, 'e647c72568b4bf9589fd5c63e8a214898769f0285839cf53987c0e47ec12e4ff', '710257', 7),
    ('4096-10', 4096, '4e4b462f767736bc27913543654afae84da78917924d4f2d60f8990a6b7a3a94', '710384', 7),
    ('4096-50', 4096, '0e349879840c9c0cf3261f3263359482be3c1b4a9383c9bd68561014acefba6f', '710511', 7),
    ('4096-90', 4096, 'c40c8baf22c4a45590d30a327475c9f2163af28cee13baef0bf290cd74779074', '710638', 7),
    ('8192-10', 8192, 'b8198a7998b20d7edd3ada87edecbce958843b6bcafcf9d4983a239c6324fa02', '710765', 7),
    ('8192-50', 8192, 'd3f37a78f3f642d958c455757b859a222ab47784de1833847a5a5954aa37abb6', '710892', 7),
    ('8192-90', 8192, '46616386557835b6e297f422fe33ed022202e321bde2729c7232a741939c9a47', '711019', 7),
    ('45056-50', 45056, '8e7d6e535925c3552adaf57319be27d987365520fa852409366c05cf32b6c423', '711146', 7),
    ('204800-50', 204800, '12ba2ea94933c44aea775a49a5380161413c7d377208ae35c8ad20fab8c6f150', '711273', 7),
)


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


def compare_evidence(baseline, candidate, quality, *,
                     policy_axis='chunk-order', plan_path=None,
                     expected_plan_sha256=None, numerical_evidence_sha256=None):
    sources = {}
    require(policy_axis in AXES, 'unknown policy axis')
    plan = None
    paths = {}
    quality_reference = QUALITY_REFERENCE
    quality_hashes = FROZEN_QUALITY_SHA256
    if policy_axis == 'partition':
        require(plan_path is not None, 'partition screen requires frozen plan')
        plan = read_bound_json(plan_path, expected_plan_sha256, sources)
        check_partition_plan(plan)
        quality_reference = Path(plan['quality_reference_path'])
        require(quality_reference.is_absolute(),
                'quality reference requires an absolute frozen path')
        quality_hashes = {
            path: digest for path, digest in FROZEN_QUALITY_SHA256.items()
            if path != str(QUALITY_REFERENCE)}
        quality_hashes[str(quality_reference)] = plan['quality_reference_sha256']
    else:
        require(plan_path is None and expected_plan_sha256 is None and
                numerical_evidence_sha256 is None,
                'partition-only evidence arguments on chunk-order axis')
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
        require(p['mode'] == mode, 'wrong run mode/order')
        check_policy_protocol(p, order, policy_axis)
        require(x['runner_rc'] == x['monitor_rc'] == 0 and not x['failure'] and
                not x['cleanup_failed'] and
                x['unit_after_cleanup']['LoadState'] == 'not-found', 'wrapper failed/unclean')
        require(e['server'] == 0 and e['http_output_checks_passed'] is True and
                not e['failure'] and not e['cleanup_failure'], 'HTTP evidence failed')
        require(capacity['matches_requested'] is True and
                capacity['requested'] == capacity['effective'] == CAPACITY,
                'capacity mismatch')
        require(command['effective_q4t_environment'] == p['effective_environment'],
                'effective policy environment mismatch')
        require(command['isolation']['host_cache_max_bytes'] == p['host_cache_max_bytes']
                and command['isolation']['swap_max_bytes'] == p['swap_max_bytes'] == 0,
                'isolation mismatch')
        identities = p['input_config_sha256']
        require(all(identities.get(path) == digest
                    for path, digest in FROZEN_CONFIG_SHA256.items()),
                'model config/index/hot identity differs from frozen screen')
        require(command['hot_list'] == {'path': str(HOT),
                'sha256': FROZEN_CONFIG_SHA256[str(HOT)]},
                'actual server hot identity differs from protocol')
        for flag, value in (('--model-dir', str(MODEL)),
                            ('--moe-hot-list', str(HOT)),
                            ('--moe-resident-slots', '256')):
            argv = command['argv']
            require(argv.count(flag) == 1 and argv[argv.index(flag) + 1] == value,
                    'server command differs from frozen screen: ' + flag)
        binary_sha = (directory / 'http/binary.sha256').read_text().strip()
        require(binary_sha == p['binary_sha256'], 'binary identity mismatch')
        if policy_axis == 'partition':
            require(p['binary_sha256'] == plan['runtime_binary_sha256'] and
                    p['tool_sha256'] == plan['tool_sha256'],
                    'partition runtime/tools differ from frozen plan')
            bound_file(p['binary'], p['binary_sha256'], sources)
            cache = Path(p['binary']).parent / 'CMakeCache.txt'
            digest = hashlib.sha256(cache.read_bytes()).hexdigest()
            bound_file(cache, digest, sources)
            bound_file(directory / 'http/CMakeCache.txt', digest, sources)
            for name, digest in p['tool_sha256'].items():
                bound_file(directory / 'tools' / name, digest, sources)
            for path, digest in identities.items():
                bound_file(path, digest, sources)
            bound_file(directory / 'http/run_acceptance.py',
                       p['tool_sha256']['run_acceptance.py'], sources)
            argv = command['argv']
            require(argv[:2] == [p['binary'], 'serve'] and
                    argv.count('--no-mtp') == 1 and '--mtp' not in argv and
                    '--no-budget' not in argv, 'server binary/MTP/budget differs')
            for key, expected in CAPACITY.items():
                flag = '--' + key.replace('_', '-')
                require(argv.count(flag) == 1 and
                        argv[argv.index(flag) + 1] == str(expected),
                        'server capacity command differs: ' + flag)
            log = directory / 'http/server.log'
            raw = log.read_bytes()
            sources[str(log)] = hashlib.sha256(raw).hexdigest()
            paths[str(directory)] = partition_path_evidence(raw.decode(), order)
        return p, x, e

    q, qexit, qe = completed(quality, 'quality', 1)
    require(q['fixtures'] == str(QUALITY_FIXTURES) and all(
            q['input_config_sha256'].get(path) == digest
            for path, digest in quality_hashes.items()),
            'quality fixture/reference differs from frozen screen')
    require(q['fixture_sha256'] == {
            Path(path).name: digest for path, digest in quality_hashes.items()
            if Path(path).parent == QUALITY_FIXTURES},
            'quality manifest/requests identity mismatch')
    quality_command = read(quality, 'runner-command.json')
    require(quality_command.count('--reference') == 1 and
            quality_command[quality_command.index('--reference') + 1] == str(quality_reference),
            'frozen quality reference was not passed to runner')
    qr = read(quality, 'http/results.json')
    require(len(qr) == 11 and qe['completed'] == 11 and
            len({r['id'] for r in qr}) == 11 and all(
                r['success'] == 1 and r['exact_match'] is True and
                r['length_match'] is True and r['finish'] == ['stop'] and
                r['requested_capacity'] == r['effective_capacity'] == CAPACITY
                for r in qr), 'fixed quality 11 contract failed')
    actual_quality = {(r['id'], r['actual_input'], r['prompt_sha256'],
                       r['text'], r['actual_output']) for r in qr}
    require(actual_quality == set(QUALITY_CASES),
            'quality IDs/prompt/text/token counts differ from frozen reference')
    runs = []
    for directory, order in [(baseline, 0), (candidate, 1)]:
        p, x, e = completed(directory, 'performance', order)
        expected_reference = PERF_REFERENCE if order == 0 else baseline / 'http/results.json'
        expected_reference_sha = (PERF_REFERENCE_SHA256 if order == 0 else
                                  sources[str(expected_reference)])
        client_command = read(directory, 'runner-command.json')
        require(client_command.count('--reference') == 1 and
                client_command[client_command.index('--reference') + 1] == str(expected_reference)
                and p['input_config_sha256'].get(str(expected_reference)) == expected_reference_sha,
                'performance reference path/hash differs from frozen off->on chain')
        require(p['fixtures'] == str(PERF_FIXTURES) and
                p['fixture_sha256'] == {'context-45056/requests.jsonl': PERF_FIXTURE_SHA256} and
                p['input_config_sha256'].get(str(PERF_FIXTURES / 'context-45056/requests.jsonl')) == PERF_FIXTURE_SHA256,
                'performance fixture differs from frozen screen')
        group = read(directory, 'runner-process-group.json')
        require(group['cleanup_complete'] is True and group['runner_reaped'] is True
                and not group['failure'] and not group['after_cleanup']['live_pids']
                and not group['after_cleanup']['errors'],
                'runner/client process group was not cleanly completed')
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
    require({k:v for k,v in bp['input_config_sha256'].items() if k != str(PERF_REFERENCE)} ==
            {k:v for k,v in cp['input_config_sha256'].items() if k != str(baseline / 'http/results.json')},
            'input configuration differs beyond the explicitly chained reference')
    require(bp['binary_sha256'] == q['binary_sha256'], 'quality used another binary')
    require(qexit['ended_t'] <= bx['started_t'] and bx['ended_t'] <= cx['started_t'],
            'frozen quality -> off -> on ordering not established')
    axis_environment = ('Q4T_MOE_PARTITION' if policy_axis == 'partition'
                        else 'Q4T_MOE_CHUNK_ORDER')
    require({k:v for k,v in bp['effective_environment'].items() if k!=axis_environment} ==
            {k:v for k,v in cp['effective_environment'].items() if k!=axis_environment},
            'environment differs beyond declared policy axis')
    require(all(a['text'] == b['text'] and a['prompt_sha256'] == b['prompt_sha256']
                for a,b in zip(br,cr)), 'candidate output/prompt differs from baseline')
    result = {'schema': 1, **screen_metrics(bm,cm), 'source_sha256': sources,
            'frozen_contract': 'offload-autonomous-20261003/plan.json',
            'evidence_contracts_passed': True, 'quality_11_passed': True,
            'scope': 'single 45056 tier, three requests per condition',
            'total_physical_RAM': 'INDETERMINATE',
            'file_cache_during_requests': 'NOT_SAMPLED'}
    if policy_axis == 'partition':
        require(plan['frozen_at'] <= qexit['started_t'],
                'partition plan was not frozen before first quality test')
        numerical = check_numerical(plan, numerical_evidence_sha256, sources,
                                   qexit['ended_t'], bx['started_t'])
        result.update(policy_axis=policy_axis, frozen_contract=str(plan_path),
                      plan_sha256=expected_plan_sha256,
                      numerical_evidence_sha256=numerical_evidence_sha256,
                      numerical=numerical, runtime_paths=paths)
        eligible = all(path['runtime_eligible'] for path in paths.values())
        result['runtime_eligibility_passed'] = eligible
        if not eligible:
            result.update(decision='NO_GO', screening_passed=False)
        for path, digest in tuple(sources.items()):
            bound_file(path, digest, sources)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('baseline', 'candidate', 'quality', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--policy-axis', choices=AXES, default='chunk-order')
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--expected-plan-sha256')
    parser.add_argument('--numerical-evidence-sha256')
    args = parser.parse_args()
    out = args.output.resolve()
    if not any(out.is_relative_to(ROOT / d) for d in ('build', '.q4t-work')):
        parser.error('output must be under build/ or .q4t-work/')
    if out.exists():
        parser.error('output already exists; earlier evidence is immutable')
    try:
        result = compare_evidence(args.baseline.resolve(), args.candidate.resolve(),
            args.quality.resolve(), policy_axis=args.policy_axis,
            plan_path=args.plan.resolve() if args.plan else None,
            expected_plan_sha256=args.expected_plan_sha256,
            numerical_evidence_sha256=args.numerical_evidence_sha256)
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

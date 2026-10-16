"""Audit the completed frozen HTTP NO_GO; no requests, builds, or GPU work."""
import hashlib
import json
import math
import statistics
import subprocess
import time
from pathlib import Path

ROOT = Path('/home/rm01/models/dev/qwen4-thor')
WORK = ROOT / '.q4t-work/gdn-record-fold-20261016'
OUT = WORK / 'http-resume'
BASE = ROOT / '.q4t-work/mtp-numerical-closure-20261016/http'
LENGTHS = [1024, 4096, 8192, 45056, 204800]


def read(path):
    return json.loads(path.read_text())


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def main():
    target = WORK / 'final-decision.json'
    require(not target.exists(), 'refuse to overwrite final decision')
    stop = read(OUT / 'gate-stop.json')
    require('NO_GO adverse performance separation' in stop['reason'],
            'this audit is only for the frozen adverse-performance exit')
    require(not (OUT / 'environment-abort.json').exists(),
            'environment abort cannot be reclassified as candidate NO_GO')
    frozen = read(OUT / 'identity/frozen/identity.json')
    for group, count in [('quality-on', 11), ('on', 5)]:
        end = read(OUT / group / 'exit.json')
        require(end['server'] == 0 and end['completed'] == count and
                end['http_output_checks_passed'] and end['failure'] is None and
                end['cleanup_failure'] is None, 'incomplete HTTP group')
        require(read(OUT / (group + '-exit.json'))['returncode'] == 0,
                'HTTP runner did not exit normally')
        monitor = read(OUT / (group + '-environment-monitor.json'))
        require(monitor['conflicts'] == [] and
                monitor['own_runner_confirmed_exited'] and
                monitor['cleanup_error'] is None and
                not monitor['sigint_sent_to_own_runner'], 'environment overlap')
        for moment in ['before', 'after']:
            require(read(OUT / 'identity' / (group + '-' + moment) /
                         'identity.json') == frozen, 'candidate identity changed')
    require(read(OUT / 'on/exit.json')['full_five_tier_completed'],
            'five-tier matrix incomplete')
    quality = read(OUT / 'quality-on/results.json')
    old_quality = read(BASE / 'quality-on/results.json')
    require(len(quality) == len(old_quality) == 11, 'quality matrix incomplete')
    for row, old in zip(quality, old_quality):
        require(row['success'] and row['exact_match'] and row['length_match'] and
                row['response_id_valid'] and row['text'] == old['text'] and
                row['prompt_sha256'] == old['prompt_sha256'], 'quality differs')
    candidate = read(OUT / 'on/results.json')
    baseline = read(BASE / 'on/results.json')
    require([row['length'] for row in candidate] == LENGTHS and
            [row['length'] for row in baseline] == LENGTHS, 'tiers differ')
    rows = []
    for new, old in zip(candidate, baseline):
        require(new['outputs'] == old['outputs'] and new['deterministic'] and
                new['prompt_sha256'] == old['prompt_sha256'], 'output differs')
        nt = [m['decode_tps'] for m in new['metrics']]
        ot = [m['decode_tps'] for m in old['metrics']]
        nf = [m['ttft'] for m in new['metrics']]
        of = [m['ttft'] for m in old['metrics']]
        require(all(len(x) == 3 for x in [nt, ot, nf, of]), 'wrong repeat count')
        ratio = statistics.harmonic_mean(nt) / statistics.harmonic_mean(ot)
        rows.append({'length': new['length'], 'decode_gain_pct': 100 * (ratio - 1),
                     'baseline_decode_tps_harmonic': statistics.harmonic_mean(ot),
                     'candidate_decode_tps_harmonic': statistics.harmonic_mean(nt),
                     'baseline_ttft_range': [min(of), max(of)],
                     'candidate_ttft_range': [min(nf), max(nf)],
                     'ttft_mean_delta_ms': 1000 * (statistics.mean(nf) - statistics.mean(of)),
                     'ttft_adverse_separation': min(nf) > max(of),
                     'decode_adverse_separation': max(nt) < min(ot),
                     'decode_favorable_separation': min(nt) > max(ot)})
    geomean_pct = 100 * (math.exp(statistics.mean(
        math.log(1 + row['decode_gain_pct'] / 100) for row in rows)) - 1)
    comparison = read(OUT / 'on-comparison.json')
    require(math.isclose(geomean_pct, 100 * comparison['decode_gain_geomean'],
                         abs_tol=1e-10), 'independent metrics differ')
    adverse = [row['length'] for row in rows if
               row['ttft_adverse_separation'] or row['decode_adverse_separation']]
    require(adverse, 'no adverse tier found')
    for group in ['quality-off', 'off']:
        require(not (OUT / group).exists(), 'unexpected extra HTTP group')
    diff = subprocess.run(['git', 'diff', '--quiet', '28a3783', '--',
                           'src', 'include', 'CMakeLists.txt'], cwd=ROOT)
    require(diff.returncode == 0, 'production runtime differs from accepted main')
    production_sha = digest(ROOT / 'build/q4t')
    require(production_sha ==
            'be74946d815904e84899c64fd88511cb0474166bef9d199c22330e8413db5072',
            'production binary differs')
    require(digest(WORK / 'build/q4t') == frozen['candidate_binary_sha256'],
            'candidate binary changed')
    result = {
        'decision': 'NO_GO', 'time_unix': time.time(), 'gate_stop': stop,
        'candidate_identity': frozen, 'formal_evidence_directory': str(OUT),
        'formal_requests': 26, 'quality_on_passed': 11,
        'performance_on_output_checks_passed': 15,
        'quality_texts_and_prompts_match_baseline': True,
        'quality_http_status_note': 'Status code may be null; success and recorded response/input/output contracts are used, not fabricated HTTP 200.',
        'performance_output_contracts_match_baseline': True,
        'decode_gain_geomean_pct': geomean_pct, 'adverse_lengths': adverse,
        'rows': rows, 'recorded_candidate_identities_equal': True,
        'observed_concurrent_gpu_processes': [],
        'environment_monitor_limit': 'Two-second best-effort /proc polling, not an atomic host-wide lock.',
        'not_run': ['quality-off', 'off-five-tier', 'checkpoint-contracts',
                    'lifecycle', 'full-model-256-step-near-tie-and-causality',
                    'HTTP-boundaries-cancellation-and-recovery'],
        'production_runtime_matches_main_28a3783': True,
        'production_binary_sha256': production_sha,
        'interpretation': 'Frozen three-sample operational NO_GO, not a statistical regression or a causal diagnosis; no numeric acceptance claimed.',
        'scope': 'One anchor/rank-one variant; no conclusion rejecting all NInfer Record/Fold designs.',
    }
    target.write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n')
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()

"""Bounded MoE site isolation; first test is evalscope HTTP, always restore."""
from pathlib import Path
import argparse
import datetime
import hashlib
import json
import os
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[3]
PREPARED = ROOT / '.q4t-work/prepared/e4m3-site-isolation-20260927'
SOURCES = ['include/q4t/quant/format.h', 'src/quant/moe_gemm.cu',
           'src/quant/moe_decode.cu']


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site', choices=['input', 'inter'], required=True)
    args = parser.parse_args()
    plan = json.loads((PREPARED / 'plan.json').read_text())
    deadline = datetime.datetime.fromisoformat(plan['deadline_utc'])
    assert datetime.datetime.now(datetime.timezone.utc) < deadline
    p = PREPARED / args.site
    p.mkdir()
    for name in SOURCES:
        assert sha(ROOT / name) == plan['sources'][name]
        assert sha(PREPARED / Path(name).name) == plan['sources'][name]
    assert sha(ROOT / 'build/q4t') == plan['baseline_sha256']
    assert sha(PREPARED / 'q4t-accepted') == plan['baseline_sha256']
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            argv = (proc / 'cmdline').read_bytes().split(b'\0')
            exe = (proc / 'exe').resolve(strict=True)
        except OSError:
            continue
        assert not (exe.name.startswith('q4t') and b'serve' in argv)
    shutil.copy2(__file__, p / 'run.py')
    try:
        header = (PREPARED / 'format.h').read_text()
        header = header.replace(
            'inline __host__ __device__ uint8_t FloatToE4m3(float v) {',
            '// Temporary HTTP site-isolation candidate; not an accepted fix.\n'
            'template <bool CorrectHigh = false>\n'
            'inline __host__ __device__ uint8_t FloatToE4m3(float v) {')
        assert header.count('if (exp > 14) {') == 1
        header = header.replace('if (exp > 14) {',
                                'if (CorrectHigh ? (exp > 15 || '
                                '(exp == 15 && man > 6))\n'
                                '                  : (exp > 14)) {')
        (ROOT / SOURCES[0]).write_text(header)
        gemm = (PREPARED / 'moe_gemm.cu').read_text()
        call = 'FloatToE4m3(block_scale / inv_scale)'
        assert gemm.count(call) == 2
        parts = gemm.split(call)
        index = 0 if args.site == 'input' else 1
        calls = [call, call]
        calls[index] = 'FloatToE4m3<true>(block_scale / inv_scale)'
        gemm = parts[0] + calls[0] + parts[1] + calls[1] + parts[2]
        (ROOT / SOURCES[1]).write_text(gemm)
        decode = (PREPARED / 'moe_decode.cu').read_text()
        assert decode.count(call) == 1
        condition = '!Down' if args.site == 'input' else 'Down'
        decode = decode.replace(call, f'FloatToE4m3<{condition}>(block_scale / inv_scale)')
        (ROOT / SOURCES[2]).write_text(decode)
        for name in SOURCES:
            shutil.copy2(ROOT / name, p / Path(name).name)
        (p / 'runtime.patch').write_bytes(subprocess.check_output(
            ['git', 'diff', '--', *SOURCES], cwd=ROOT))
        command = ['cmake', '--build', 'build', '--parallel', '4']
        save(p / 'build-command.json', command)
        with (p / 'build.log').open('w') as log:
            code = subprocess.run(command, cwd=ROOT, stdout=log,
                                  stderr=subprocess.STDOUT).returncode
        warnings = (p / 'build.log').read_text().lower().count('warning:')
        save(p / 'build-result.json', {'exit': code, 'warnings': warnings,
             'binary_sha256': sha(ROOT / 'build/q4t'),
             'source_sha256': {n: sha(ROOT / n) for n in SOURCES}})
        assert code == 0 and warnings == 0
        shutil.copy2(ROOT / 'build/q4t', p / 'q4t-candidate')
        out = ROOT / f'.q4t-work/e2e/e4m3-{args.site}-only-http-20260927'
        fixtures = ROOT / '.q4t-work/prepared/e4m3-rounding-20260923/recovery-inputs'
        command = ['python3', 'tools/evalscope/run_acceptance.py',
                   '--mode', 'quality', '--output', str(out),
                   '--model-dir', str(Path.home() / 'models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream'),
                   '--fixtures', str(fixtures), '--startup-timeout', '600']
        save(p / 'command.json', command)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
        with (p / 'driver.log').open('w') as log:
            code = subprocess.run(command, cwd=ROOT, env=env, stdout=log,
                                  stderr=subprocess.STDOUT).returncode
        save(p / 'driver-exit.json', {'exit': code})
        terminal = json.loads((out / 'exit.json').read_text())
        results = json.loads((out / 'results.json').read_text())
        assert terminal['server'] == 0 and terminal['completed'] == len(results) == 3
        baseline = json.loads((ROOT / '.q4t-work/e2e/e4m3-high-only-recovery-20260923/results.json').read_text())
        scores = []
        for row, old in zip(results, baseline):
            assert row['id'] == old['id'] and row['prompt_sha256'] == old['prompt_sha256']
            assert row['success'] and row['length_match'] and row['finish'] == ['stop']
            scores.append({'id': row['id'], 'expected': row['expected'],
                           'actual': row['text'], 'baseline': old['text'],
                           'correct': row['exact_match'],
                           'new_failure': old['exact_match'] and not row['exact_match']})
        report = {'site': args.site, 'scores': scores, 'driver_exit': code,
                  'new_failures': [r['id'] for r in scores if r['new_failure']],
                  'accepted': False, 'full_quality_run': False,
                  'performance_run': False, 'numeric_tests_run': False}
        save(p / 'decision.json', report)
        print(json.dumps(report, indent=2), flush=True)
    finally:
        for name in SOURCES:
            shutil.copy2(PREPARED / Path(name).name, ROOT / name)
        shutil.copy2(PREPARED / 'q4t-accepted', ROOT / 'build/q4t')
        assert sha(ROOT / 'build/q4t') == plan['baseline_sha256']
        assert all(sha(ROOT / n) == plan['sources'][n] for n in SOURCES)
        save(p / 'restored.json', {'binary_sha256': sha(ROOT / 'build/q4t'),
                                  'sources_restored': True,
                                  'recovery_http_pending': True})


if __name__ == '__main__':
    main()

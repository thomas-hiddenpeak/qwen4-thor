"""Run the frozen paired screening through the existing evalscope HTTP driver."""
from pathlib import Path
import datetime
import hashlib
import json
import os
import signal
import subprocess

ROOT = Path(__file__).resolve().parents[3]
P = ROOT / '.q4t-work/prepared/quality-review-v2-20260927'
OUT = ROOT / '.q4t-work/e2e/quality-review-v2-20260927'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def main():
    plan = json.loads((P / 'plan.json').read_text())
    deadline = datetime.datetime.fromisoformat(plan['deadline_utc'])
    assert datetime.datetime.now(datetime.timezone.utc) < deadline
    assert json.loads((P / 'fixture-audit.json').read_text())['new_answers_verified_from_rendered_prompts_using_sql'] == 120
    for name, expected in plan['bindings'].items():
        assert sha(ROOT / name) == expected, name
    assert sha(ROOT / 'build/q4t') == plan['baseline_sha256']
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            argv = (proc / 'cmdline').read_bytes().split(b'\0')
            exe = (proc / 'exe').resolve(strict=True)
        except OSError:
            continue
        assert not (exe.name.startswith('q4t') and b'serve' in argv)
    OUT.mkdir()
    save(OUT / 'plan.json', plan)
    stages = [('baseline-first', 'baseline', 'baseline-first'),
              ('fixed', 'fixed', 'all'), ('baseline-last', 'baseline', 'baseline-last')]
    env = {k: v for k, v in os.environ.items() if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
    records = []
    for stage, binary_name, fixture in stages:
        assert datetime.datetime.now(datetime.timezone.utc) < deadline
        out = OUT / stage
        command = ['python3', 'tools/evalscope/run_acceptance.py', '--mode', 'quality',
                   '--output', str(out), '--binary', str(P / ('q4t-' + binary_name)),
                   '--model-dir', plan['model_dir'], '--fixtures', str(P / fixture),
                   '--startup-timeout', '600']
        save(OUT / (stage + '-command.json'), command)
        print('Starting ' + stage, flush=True)
        with (OUT / (stage + '-driver.log')).open('w') as log:
            proc = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log,
                                    stderr=subprocess.STDOUT, start_new_session=True)
            try:
                code = proc.wait(timeout=(deadline - datetime.datetime.now(datetime.timezone.utc)).total_seconds())
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=25)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                save(OUT / 'deadline.json', {'stage': stage, 'deadline_reached': True,
                                           'decision': 'incomplete_not_accepted'})
                raise
        save(OUT / (stage + '-driver-exit.json'), {'exit': code})
        expected_count = len(json.loads((P / fixture / 'manifest.json').read_text()))
        terminal = json.loads((out / 'exit.json').read_text())
        assert terminal['server'] == 0 and terminal['completed'] == expected_count
        assert code in (0, 1)
        assert terminal['failure'] in (None, 'RuntimeError: quality acceptance failed')
        results = json.loads((out / 'results.json').read_text())
        assert len(results) == expected_count
        assert all(r['success'] and r['length_match'] for r in results)
        records.append({'stage': stage, 'completed': expected_count,
                        'server_exit': terminal['server'], 'driver_exit': code,
                        'binary_sha256': sha(P / ('q4t-' + binary_name))})
        save(OUT / 'progress.json', records)
        print('Completed ' + stage + ': ' + str(expected_count) + ' HTTP records; scores withheld until paired analysis.', flush=True)
    assert sha(ROOT / 'build/q4t') == plan['baseline_sha256']
    save(OUT / 'collection.json', {'complete': True, 'records': records,
                                  'production_binary_changed': False,
                                  'performance_claim': False})


if __name__ == '__main__':
    main()

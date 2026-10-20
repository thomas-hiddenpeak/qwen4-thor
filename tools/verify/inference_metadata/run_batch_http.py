"""Frozen quality/cancellation stage, conditional on the exact replay passing."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parents[3]
STAGE = ROOT / '.q4t-work/metadata-followup-20261020'
OUT = STAGE / 'batch-http-v1'
REFERENCE = ROOT / '.q4t-work/inference-metadata-20261020/resume-acceptance-v1'
MODEL = '/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream'
EXPECTED = '753d9769f96e9848800eae88b4800bef65e20885fd2ec025b8f3bc91fc497542'


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2) + '\n')


def main():
    state = dict(status='PREPARING', started=time.time(), completed=[])
    try:
        replay = STAGE / 'batch-replay-v1'
        if json.loads((replay / 'run.json').read_text())['status'] != 'PASS':
            raise ValueError('replay did not pass')
        if json.loads((replay / 'lease/exit.json').read_text())['status'] != 'COMPLETED':
            raise ValueError('replay guard incomplete')
        if subprocess.check_output(['git', 'diff', 'b6870c0', '--', 'src', 'include'], cwd=ROOT):
            raise ValueError('runtime source changed')
        candidate = OUT / 'candidate-build'
        candidate.mkdir()
        for name in ['q4t', 'CMakeCache.txt']:
            shutil.copy2(STAGE / 'build' / name, candidate / name)
        binary = candidate / 'q4t'
        if sha(binary) != EXPECTED:
            raise ValueError('candidate identity changed')
        manifest = json.loads((REFERENCE / 'sealed-files.json').read_text())
        for mode in ['off', 'on']:
            for name in ['inputs/manifest.json', 'inputs/requests.jsonl', 'results.json']:
                source = REFERENCE / ('quality-' + mode) / name
                if sha(source) != manifest[str(source.relative_to(REFERENCE))]:
                    raise ValueError('reference changed')
            target = OUT / ('reference-' + mode)
            target.mkdir()
            shutil.copytree(REFERENCE / ('quality-' + mode) / 'inputs', target / 'inputs')
            shutil.copy2(REFERENCE / ('quality-' + mode) / 'results.json', target / 'results.json')
        shutil.copy2(Path(__file__), OUT / 'driver.py')
        save('plan.json', dict(candidate_source='b6870c0', binary_sha256=EXPECTED,
             quality_requests=22, cancellation_generation_posts=20,
             cancellation_control_posts=9, model_loads=3, retries=0,
             performance_requests=0, default_binary_sha256=sha(ROOT / 'build/q4t')))
        env = {k: v for k, v in os.environ.items()
               if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}

        def run(name, command):
            if sha(binary) != EXPECTED:
                raise ValueError('candidate changed')
            state.update(status='RUNNING', current=name)
            save('run.json', state)
            save(name + '-command.json', command)
            with (OUT / (name + '-driver.log')).open('w') as log:
                subprocess.run(command, cwd=ROOT, env=env, stdout=log,
                               stderr=subprocess.STDOUT, timeout=1800, check=True)
            state['completed'].append(name)
            save('run.json', state)

        for mode in ['off', 'on']:
            name = 'quality-' + mode
            ref = OUT / ('reference-' + mode)
            command = ['python3', '-B', 'tools/evalscope/run_acceptance.py',
                       '--mode', 'quality', '--output', str(OUT / name),
                       '--binary', str(binary), '--model-dir', MODEL,
                       '--fixtures', str(ref / 'inputs'), '--reference',
                       str(ref / 'results.json'), '--port', '8740',
                       '--startup-timeout', '300', '--allow-unqualified-binary']
            if mode == 'on':
                command.append('--mtp')
            run(name, command)
            rows = json.loads((OUT / name / 'results.json').read_text())
            shutdown = json.loads((OUT / name / 'exit.json').read_text())
            if len(rows) != 11 or not all(row['success'] == 1 and row['exact_match']
                                        and row['length_match'] for row in rows):
                raise ValueError('quality incomplete')
            if not shutdown['http_output_checks_passed'] or shutdown['server'] != 0:
                raise ValueError('quality shutdown failed')
        run('cancellation', ['python3', '-B', 'tools/evalscope/run_request_cancellation.py',
            '--binary', str(binary), '--output', str(OUT / 'cancellation'),
            '--quality-run', str(OUT / 'quality-off'), '--performance-run',
            str(REFERENCE / 'candidate-off')])
        cancellation = json.loads((OUT / 'cancellation/summary.json').read_text())
        if cancellation['failure'] or cancellation['server_exit'] != 0:
            raise ValueError('cancellation failed')
        state.update(status='PASS_RUNTIME_CONTRACTS', performance_acceptance=False,
                     deployed=False)
    except BaseException as error:
        state.update(status='FAILED_OR_INTERRUPTED', error=repr(error))
        raise
    finally:
        state['finished'] = time.time()
        save('run.json', state)


if __name__ == '__main__':
    main()

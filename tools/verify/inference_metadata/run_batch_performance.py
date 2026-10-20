"""Frozen 60-request comparison; invoke through seal_stage, no retries."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import time

from run_batch_replay import check_model

ROOT = Path(__file__).resolve().parents[3]
STAGE = ROOT / '.q4t-work/metadata-followup-20261020'
OUT = STAGE / 'batch-performance-v1'
PRIOR = ROOT / '.q4t-work/inference-metadata-20261020/resume-acceptance-v1'
PACKAGE = STAGE / 'final-review-v1'
MODEL = '/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream'
HASHES = {
    'parent': 'c8f4b241f67b2818e92791f688082e338f6981a5db923cf09fb07e30b99e887d',
    'candidate': '753d9769f96e9848800eae88b4800bef65e20885fd2ec025b8f3bc91fc497542',
}
LENGTHS = [1024, 4096, 8192, 45056, 204800]
ORDER = ['parent-off', 'candidate-off', 'candidate-on', 'parent-on']


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2) + '\n')


def verify_seal(directory, names=None):
    manifest = json.loads((directory / 'sealed-files.json').read_text())
    for name in manifest if names is None else names:
        if sha(directory / name) != manifest[name]:
            raise ValueError('sealed evidence changed: ' + str(directory / name))


def compare(mode):
    rows = {label: json.loads((OUT / (label + '-' + mode) / 'results.json')
                             .read_text()) for label in HASHES}
    for label in HASHES:
        if [r['length'] for r in rows[label]] != LENGTHS:
            raise ValueError('incomplete matrix: ' + label)
    comparisons = []
    for parent, candidate in zip(rows['parent'], rows['candidate']):
        if any(parent[key] != candidate[key]
               for key in ['length', 'prompt_sha256', 'outputs']):
            raise ValueError('parent/candidate input or output differs')
        if any(len(row['metrics']) != 3 for row in [parent, candidate]):
            raise ValueError('incomplete request repeats')
        row = {'length': parent['length'], 'parent': parent['metrics'],
               'candidate': candidate['metrics']}
        for metric in ['ttft', 'decode_tps']:
            a = statistics.median(x[metric] for x in parent['metrics'])
            b = statistics.median(x[metric] for x in candidate['metrics'])
            row[metric + '_parent_median'] = a
            row[metric + '_candidate_median'] = b
            row[metric + '_median_change_pct'] = 100 * (b / a - 1)
        comparisons.append(row)
    return comparisons


def main():
    state = dict(status='PREPARING', started=time.time(), completed=[])
    try:
        if subprocess.check_output(['git', 'diff', 'b6870c0', '--', 'src', 'include'],
                                   cwd=ROOT):
            raise ValueError('candidate runtime changed')
        if sha(ROOT / 'build/q4t') != HASHES['parent']:
            raise ValueError('default identity changed')
        verify_seal(STAGE / 'batch-http-v1')
        verify_seal(STAGE / 'batch-replay-v1', ['model-files.json'])
        model_records = json.loads((STAGE / 'batch-replay-v1/model-files.json').read_text())
        check_model(model_records)
        save('model-files.json', model_records)
        package_manifest = json.loads((PACKAGE / 'prepared-files.json').read_text())
        for name, digest in package_manifest.items():
            if sha(PACKAGE / name) != digest:
                raise ValueError('prepared package changed: ' + name)
        binaries = {}
        for label, folder in [('parent', 'previous'), ('candidate', 'candidate')]:
            target = OUT / (label + '-build')
            target.mkdir()
            for name in ['q4t', 'CMakeCache.txt']:
                shutil.copy2(PACKAGE / folder / name, target / name)
            binaries[label] = target / 'q4t'
            if sha(binaries[label]) != HASHES[label]:
                raise ValueError('binary identity changed')
        fixtures = OUT / 'performance-fixtures'
        for length in LENGTHS:
            name = f'performance-fixtures/context-{length}/requests.jsonl'
            verify_seal(PRIOR, [name])
            target = fixtures / f'context-{length}'
            target.mkdir(parents=True)
            shutil.copy2(PRIOR / name, target / 'requests.jsonl')
        save('plan.json', dict(parent_source='3d5564e', candidate_source='b6870c0',
             binary_sha256=HASHES, compute_backend='nvml', order=ORDER,
             lengths=LENGTHS, repeats=3, output_tokens=256, loads=4,
             performance_requests=60, quality_requests=0, retries=0,
             warmup_requests=0, discard_first=False, timeout_per_start=7200,
             startup_timeout=300, max_seq=1, max_prefill=8192, max_len=208896,
             statistical_acceptance=False, tolerance=None,
             limitation='one service start per identity and mode; order/time confounded'))
        shutil.copy2(Path(__file__), OUT / 'driver.py')
        shutil.copy2(Path(__file__).with_name('run_batch_replay.py'),
                     OUT / 'model-identity-helper.py')
        shutil.copy2(ROOT / 'docs/METADATA_BATCH_PERFORMANCE_2026-10-20.md',
                     OUT / 'frozen-plan.md')
        env = {k: v for k, v in os.environ.items()
               if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
        observations = {}
        for name in ORDER:
            check_model(model_records)
            for label, binary in binaries.items():
                if sha(binary) != HASHES[label]:
                    raise ValueError('binary changed: ' + label)
            label, mode = name.split('-')
            command = ['python3', '-B', 'tools/evalscope/run_acceptance.py',
                       '--mode', 'performance', '--output', str(OUT / name),
                       '--binary', str(binaries[label]), '--model-dir', MODEL,
                       '--fixtures', str(fixtures), '--port', '8740',
                       '--startup-timeout', '300', '--max-len', '208896',
                       '--perf-repeats', '3', '--allow-unqualified-binary']
            if mode == 'on':
                command.append('--mtp')
            other = ('candidate' if label == 'parent' else 'parent') + '-' + mode
            if other in state['completed']:
                command.extend(['--reference', str(OUT / other / 'results.json')])
            state.update(status='RUNNING', current=name)
            save('run.json', state)
            save(name + '-command.json', command)
            with (OUT / (name + '-driver.log')).open('w') as log:
                subprocess.run(command, cwd=ROOT, env=env, stdout=log,
                               stderr=subprocess.STDOUT, timeout=7200, check=True)
            shutdown = json.loads((OUT / name / 'exit.json').read_text())
            if (shutdown['server'] != 0 or not shutdown['http_output_checks_passed']
                    or not shutdown['full_five_tier_completed']
                    or shutdown['completed'] != 5):
                raise ValueError('incomplete performance/shutdown: ' + name)
            state['completed'].append(name)
            if other in state['completed']:
                observations[mode] = compare(mode)
                save('observations.json', observations)
            save('run.json', state)
        check_model(model_records)
        if sha(ROOT / 'build/q4t') != HASHES['parent']:
            raise ValueError('default unexpectedly changed')
        state.update(status='MEASUREMENTS_COMPLETE_ACCEPTANCE_PENDING',
                     statistical_performance='INSUFFICIENT_EVIDENCE', deployed=False)
    except BaseException as error:
        state.update(status='FAILED_OR_INTERRUPTED', error=repr(error))
        raise
    finally:
        state['finished'] = time.time()
        save('run.json', state)


if __name__ == '__main__':
    main()

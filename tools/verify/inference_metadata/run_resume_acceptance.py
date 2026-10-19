"""Frozen post-capacity candidate acceptance; one outer NVML GPU lease."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import time

ROOT = Path(__file__).resolve().parents[3]
STAGE = ROOT / '.q4t-work/inference-metadata-20261020'
OUT = STAGE / 'resume-acceptance-v1'
PRIOR = ROOT / '.q4t-work/b1b-20261019'
MODEL = '/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream'


def sha(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def save(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2) + '\n')


def main():
    state = dict(status='PREPARING', started=time.time(), completed=[])
    try:
        identity = json.loads((ROOT / 'docs/evidence/inference-metadata-20261020/'
                               'capacity-build-v2/identity.json').read_text())
        candidate = STAGE / 'build/q4t'
        expected = identity['sha256'][str(candidate.relative_to(ROOT))]
        if sha(candidate) != expected:
            raise ValueError('candidate identity changed')
        if subprocess.check_output(['git', 'diff', '3d5564e', '--', 'src',
                                    'include', 'CMakeLists.txt'], cwd=ROOT):
            raise ValueError('runtime source differs from built candidate')
        # Verify archived parent runtime source, independently of candidate.
        parent_source = STAGE / 'parent-source'
        paths = subprocess.check_output(
            ['git', 'ls-tree', '-r', '--name-only', '73aab36', 'src', 'include'],
            cwd=ROOT, text=True).splitlines()
        for name in paths:
            original = subprocess.check_output(['git', 'show', '73aab36:' + name],
                                               cwd=ROOT)
            if (parent_source / name).read_bytes() != original:
                raise ValueError('parent source mismatch: ' + name)
        binaries = {}
        for label, source in [('candidate', candidate),
                              ('parent', STAGE / 'parent-build/q4t')]:
            target = OUT / (label + '-build')
            target.mkdir()
            for filename in ['q4t', 'CMakeCache.txt']:
                shutil.copy2(source.parent / filename, target / filename)
            binaries[label] = target / 'q4t'
        references = {}
        for mode in ['off', 'on']:
            target = OUT / ('reference-' + mode)
            target.mkdir()
            source = PRIOR / ('quality-' + mode)
            shutil.copytree(source / 'inputs', target / 'inputs')
            shutil.copy2(source / 'results.json', target / 'results.json')
            references[mode] = target
        fixtures = OUT / 'performance-fixtures'
        fixtures.mkdir()
        for length in [1024, 4096, 8192, 45056, 204800]:
            target = fixtures / ('context-' + str(length))
            target.mkdir()
            shutil.copy2(PRIOR / 'candidate-off' / target.name / 'requests.jsonl',
                         target / 'requests.jsonl')
        hashes = {label: sha(path) for label, path in binaries.items()}
        save('plan.json', dict(candidate_source='3d5564e', parent_source='73aab36',
             compute_backend='nvml', binary_sha256=hashes, loads=7,
             quality_requests=22, cancellation_generation_posts=20,
             cancellation_posts=9, performance_requests=60, retries=0,
             resolution_band='NOT_ESTABLISHED'))
        shutil.copy2(Path(__file__), OUT / 'driver.py')
        env = {key: value for key, value in os.environ.items()
               if not key.startswith('Q4T_') and key != 'LD_PRELOAD'}

        def run(name, command, timeout):
            for label, path in binaries.items():
                if sha(path) != hashes[label]:
                    raise ValueError('binary changed: ' + label)
            state.update(status='RUNNING', current=name)
            save('run.json', state)
            with (OUT / (name + '-driver.log')).open('w') as log:
                subprocess.run(command, cwd=ROOT, env=env, stdout=log,
                               stderr=subprocess.STDOUT, timeout=timeout, check=True)
            state['completed'].append(name)
            save('run.json', state)

        def acceptance(name, label, mode, kind, fixture):
            command = ['python3', '-B', 'tools/evalscope/run_acceptance.py',
                       '--mode', kind, '--output', str(OUT / name),
                       '--binary', str(binaries[label]), '--model-dir', MODEL,
                       '--fixtures', str(fixture), '--port', '8740',
                       '--startup-timeout', '300', '--allow-unqualified-binary']
            if mode == 'on':
                command.append('--mtp')
            if kind == 'quality':
                command.extend(['--reference', str(references[mode] / 'results.json')])
            return command

        for mode in ['off', 'on']:
            name = 'quality-' + mode
            run(name, acceptance(name, 'candidate', mode, 'quality',
                                 references[mode] / 'inputs'), 1800)
            rows = json.loads((OUT / name / 'results.json').read_text())
            if len(rows) != 11 or not all(r['success'] == 1 and r['exact_match']
                                        and r['length_match'] for r in rows):
                raise ValueError('incomplete quality: ' + mode)
        run('cancellation', ['python3', '-B',
            'tools/evalscope/run_request_cancellation.py', '--binary',
            str(binaries['candidate']), '--output', str(OUT / 'cancellation'),
            '--quality-run', str(OUT / 'quality-off'), '--performance-run',
            str(PRIOR / 'candidate-off')], 1800)
        cancellation = json.loads((OUT / 'cancellation/summary.json').read_text())
        if cancellation['failure'] or cancellation['server_exit'] != 0:
            raise ValueError('cancellation failed')
        for name in ['parent-off', 'candidate-off', 'candidate-on', 'parent-on']:
            label, mode = name.split('-')
            run(name, acceptance(name, label, mode, 'performance', fixtures), 7200)
        observations = {}
        for mode in ['off', 'on']:
            parent = json.loads((OUT / ('parent-' + mode) / 'results.json').read_text())
            candidate_rows = json.loads((OUT / ('candidate-' + mode) / 'results.json').read_text())
            if len(parent) != 5 or len(candidate_rows) != 5:
                raise ValueError('incomplete performance matrix')
            comparisons = []
            for a, b in zip(parent, candidate_rows):
                if any(a[key] != b[key] for key in ['length', 'prompt_sha256', 'outputs']):
                    raise ValueError('parent/candidate input or output mismatch')
                if len(a['metrics']) != 3 or len(b['metrics']) != 3:
                    raise ValueError('incomplete repeats')
                row = dict(length=a['length'], parent=a['metrics'], candidate=b['metrics'])
                for metric in ['ttft', 'decode_tps']:
                    av = statistics.median(x[metric] for x in a['metrics'])
                    bv = statistics.median(x[metric] for x in b['metrics'])
                    row[metric + '_median_change_pct'] = 100 * (bv / av - 1)
                comparisons.append(row)
            observations[mode] = comparisons
        save('observations.json', observations)
        state.update(status='MEASUREMENTS_COMPLETE_ACCEPTANCE_PENDING',
                     statistical_performance='INSUFFICIENT_EVIDENCE')
    except BaseException as error:
        state.update(status='FAILED_OR_INTERRUPTED', error=repr(error))
        raise
    finally:
        state['finished'] = time.time()
        save('run.json', state)


if __name__ == '__main__':
    main()

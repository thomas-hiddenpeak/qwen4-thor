"""Freeze the corrected offline relation check and one conditional Nsight run."""
import importlib.util
import json
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / '.q4t-work/suffix-callback-pairing-20261026'
OLD = ROOT / '.q4t-work/suffix-memset-link-20261026'
SPEC = importlib.util.spec_from_file_location(
    'prepare_common', Path(__file__).with_name('prepare_mechanism.py'))
common = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(common)
sha, require, load = common.sha, common.require, common.load
CHANGED = ('mechanism_analyze.py', 'test_mechanism_analyze.py',
           'mechanism_stage.py', 'test_mechanism_stage.py')


def main():
    require(not (BASE/'frozen-plan.json').exists(), 'one plan only')
    scope = load(BASE/'scope.json')
    require(time.time() < scope['created_unix'] + 1800, 'work wall exhausted')
    require(scope['build_attempts'] == 0 and scope['cupti_only_processes'] == 0 and
            scope['nsys_processes'] == 1, 'no extra build or capture allowed')
    old_plan_path = OLD/'frozen-plan.json'
    require(sha(old_plan_path) == scope['frozen_inputs'][str(old_plan_path)],
            'original plan changed')
    old = load(old_plan_path)
    changed = {str(ROOT/'tools/mtp_suffix_nsys'/name) for name in CHANGED}
    for path, digest in old['files'].items():
        original = OLD/'archive-final/source'/Path(path).relative_to(ROOT) if path in changed else Path(path)
        require(sha(original) == digest, 'original dependency changed: ' + path)
    for path, digest in scope['frozen_inputs'].items():
        original = OLD/'archive-final/source'/Path(path).relative_to(ROOT) if path in changed else Path(path)
        require(sha(original) == digest, 'frozen input changed: ' + path)
    cpu = load(BASE/'cpu-proof.json')
    require(set(cpu['test_modules']) == {'test_mechanism_analyze.py', 'test_mechanism_stage.py'}
            and 13 <= cpu['tests'] <= 24 and cpu['tests'] == sum(cpu['test_modules'].values())
            and cpu['suite_runs'] <= 2 and cpu['elapsed_seconds'] <= 120
            and not any(cpu[k] for k in ('failed', 'errors', 'skipped')),
            'affected CPU contracts not met')
    require(all(sha(p) == v for p, v in cpu['files'].items()), 'tested sources changed')
    proof = load(BASE/'offline-proof.json')
    raw = OLD/'run/cupti/server.log'
    analysis = BASE/'cupti-analysis.json'
    require(proof['runs'] == 1 and proof['elapsed_seconds'] <= 60 and
            proof['raw_sha256'] == sha(raw) and proof['analysis_sha256'] == sha(analysis) and
            proof['analyzer_sha256'] == sha(ROOT/'tools/mtp_suffix_nsys/mechanism_analyze.py') and
            proof['binary_sha256'] == old['binary_sha256'] == sha(old['binary']),
            'offline analysis identity differs')
    result = load(analysis)
    require(result['status'] == 'VALID_AND_REPRODUCED' and result['eligible_nsys'] is True
            and result['local_mechanism_valid'] is True and not result['errors'],
            'fixed capture not qualified for local Nsight comparison')
    paths = set(old['files']) | set(cpu['files'])
    paths.update(str(ROOT/'tools/mtp_suffix_nsys'/name) for name in CHANGED)
    paths.update(str(p) for p in (Path(__file__), BASE/'scope.json', BASE/'cpu-proof.json',
        BASE/'offline-proof.json', analysis, raw, old_plan_path, OLD/'terminal-review.json',
        OLD/'early-source-copy.json'))
    paths.update(str(OLD/'archive-final/source'/Path(p).relative_to(ROOT)) for p in changed)
    for key in ('cpu_reuse', 'prior_phase_result'):
        ref = load(OLD/'scope.json')[key]
        require(sha(ref['path']) == ref['sha256'], 'indirect prior evidence changed')
        paths.add(ref['path'])
    reused = dict(raw=str(raw), analysis=str(analysis), proof=str(BASE/'offline-proof.json'))
    reused.update({key+'_sha256': sha(path) for key, path in list(reused.items())})
    plan = {**old, 'schema': 2, 'created_unix': time.time(), 'scope': str(BASE/'scope.json'),
        'output': str(BASE/'run'), 'lease': str(BASE/'lease'), 'reused_cupti': reused,
        'cpu_tests': cpu['tests'], 'unchanged_cpu_methods_reused': 42,
        'files': {p: sha(p) for p in sorted(paths)}}
    with (BASE/'frozen-plan.json').open('x') as stream:
        json.dump(plan, stream, indent=2)
        stream.write('\n')
    print(json.dumps(dict(plan=str(BASE/'frozen-plan.json'),
        sha256=sha(BASE/'frozen-plan.json'), files=len(plan['files']), cpu_tests=cpu['tests'])))


if __name__ == '__main__':
    main()

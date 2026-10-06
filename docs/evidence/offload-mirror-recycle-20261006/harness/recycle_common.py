"""Frozen identities and existing bounded process ownership for one candidate."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import time

sys.dont_write_bytecode = True
R = Path(__file__).resolve().parent
W = R / 'source'
spec = importlib.util.spec_from_file_location(
    'recycle_owned', R.parent / 'offload-mechanism-20261006/phase_common.py')
owned = importlib.util.module_from_spec(spec)
spec.loader.exec_module(owned)
owned.W = W
read, save, sha, run = owned.read, owned.save, owned.sha, owned.run
environment = owned.environment


def frozen(expected):
    assert sha(R / 'execution-plan.json') == expected
    plan = read(R / 'execution-plan.json')
    assert plan['service_count'] == 17 and plan['http_count'] == 131
    assert [g['id'] for g in plan['groups']] == plan['group_ids']
    assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=W,
        text=True).strip() == plan['runtime_source_commit']
    assert not subprocess.check_output(['git', 'status', '--porcelain'],
                                       cwd=W).strip()
    for path, digest in plan['frozen_files'].items():
        assert Path(path).is_absolute() and sha(path) == digest, path
    build = read(R / 'build-identity.json')
    assert build['source_commit'] == plan['runtime_source_commit']
    assert build['warnings'] == 0 and build['build_rc'] == 0
    assert sha(plan['runtime_binary_path']) == plan['runtime_binary_sha256']
    assert build['binary_sha256'] == plan['runtime_binary_sha256']
    for path, digest in build['source_sha256'].items():
        assert sha(path) == digest, path
    dependency = read(R / 'dependency-admission.json')
    for path, digest in dependency['source_sha256'].items():
        assert sha(path) == digest, path
    for path, target in dependency['required_path_resolutions'].items():
        assert str(Path(path).resolve()) == target, path
    return plan


def admitted(plan, expected):
    record = read(R / 'execution-admission.json')
    assert record['execution_admitted'] is True
    assert record['execution_plan_sha256'] == expected
    assert record['runtime_binary_sha256'] == plan['runtime_binary_sha256']
    for path, digest in record['source_sha256'].items():
        assert sha(path) == digest, path
    return record


def passed(name, plan, expected):
    record = read(R / (name + '-decision.json'))
    assert record['passed'] is True and record['plan_sha256'] == expected
    assert record['runtime_binary_sha256'] == plan['runtime_binary_sha256']
    assert record['runtime_source_commit'] == plan['runtime_source_commit']
    assert record['recorded_t'] <= time.time()
    for path, digest in record.get('source_sha256', {}).items():
        assert sha(path) == digest, path
    return record

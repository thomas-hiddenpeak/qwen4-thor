"""Identity and bounded ownership for the observer appendix."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time

sys.dont_write_bytecode = True
R = Path(__file__).resolve().parent
O = R / 'observer-source'
M = R.parent / 'offload-mechanism-20261006'
spec = importlib.util.spec_from_file_location('owned_phase_common', M / 'phase_common.py')
owned = importlib.util.module_from_spec(spec)
spec.loader.exec_module(owned)
# Only unchanged run/cleanup helpers are used; their workspace scope is O.
owned.W = O
read, save, sha, run = owned.read, owned.save, owned.sha, owned.run


def frozen(expected):
    assert sha(R / 'observer-execution-plan.json') == expected
    plan = read(R / 'observer-execution-plan.json')
    for path, digest in plan['frozen_files'].items():
        assert Path(path).is_absolute() and sha(path) == digest, path
    assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=O,
                                   text=True).strip() == plan['runtime_source_commit']
    assert not subprocess.check_output(['git', 'status', '--porcelain'], cwd=O).strip()
    build = read(R / 'observer-build-identity.json')
    assert build['source_commit'] == plan['runtime_source_commit']
    assert build['warnings'] == 0 and build['build_rc'] == 0
    assert sha(plan['runtime_binary_path']) == plan['runtime_binary_sha256']
    assert build['binary_sha256'] == plan['runtime_binary_sha256']
    for path, digest in build['source_sha256'].items():
        assert sha(path) == digest, path
    dependency = read(R / 'observer-dependency-admission.json')
    for path, digest in dependency['source_sha256'].items():
        assert sha(path) == digest, path
    for path, target in dependency['required_path_resolutions'].items():
        assert str(Path(path).resolve()) == target, path
    assert plan['service_count'] == 5 and plan['http_count'] == 27
    assert plan['group_ids'] == ['q01-quality-on','s01-as-off','s02-as-on',
                                 's03-al-on','s04-al-off']
    return plan


def admitted(plan, expected):
    record = read(R / 'observer-execution-admission.json')
    assert record['execution_admitted'] is True
    assert record['execution_plan_sha256'] == expected
    for path, digest in record['source_sha256'].items():
        assert sha(path) == digest, path
    assert record['runtime_binary_sha256'] == plan['runtime_binary_sha256']
    return record


def passed(name, plan, expected):
    path = R / (name + '-decision.json')
    record = read(path)
    assert record['passed'] is True and record['plan_sha256'] == expected
    assert record['runtime_binary_sha256'] == plan['runtime_binary_sha256']
    assert record['recorded_t'] <= time.time()
    for source, digest in record['source_sha256'].items():
        assert sha(source) == digest, source
    return record

"""Seal tool acceptance and update metadata for the already-tested binary."""
import argparse
import copy
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[3]
BASE = ROOT / '.q4t-work/audit-p2-20261031/stage3'
PRIOR = BASE.parent / 'stage2'
PACKAGE = BASE / 'delivery-package'
OUTPUT = BASE / 'metadata'
MARKER = ROOT / 'build/audit-p2-final-recovery.json'
sys.path.insert(0, str(ROOT / 'tools/verify/startup_delivery'))
import deploy as h


def verify_runtime():
    source = h.read(PRIOR / 'frozen/runtime-source.json')
    actual = {str(p.relative_to(ROOT)): h.sha(p) for folder in ['src', 'include']
              for p in (ROOT / folder).rglob('*') if p.is_file()}
    h.require(actual == source, 'accepted runtime changed')


def prepare():
    verify_runtime()
    prior = h.read(PRIOR / 'deployment/finalized.json')
    h.require(prior['status'] == 'DELIVERED', 'runtime stage not delivered')
    old = h.verify_pair(ROOT / 'build/q4t', ROOT / 'build/q4t.release.json',
                        prior['binary_sha256'])
    h.require(h.sha(ROOT / 'build/q4t.release.json') == prior['sidecar_sha256'],
              'installed metadata changed')
    history = {}
    for stage in ['stage1', 'stage2']:
        for name in ['delivery-package', 'evidence-archive', 'health-archive']:
            history[stage + '/' + name] = h.verify_seal(BASE.parent / stage / name)
    cpu = BASE / 'cpu'
    cpu.mkdir()
    names = ['stage3-host-configure.log', 'stage3-host-build.log',
             'stage3-host-ctest.log', 'stage3-unsupported-configure.log']
    for name in names:
        h.copy_checked(h.identity(BASE.parent / name), cpu / name)
    h.copy_checked(h.identity(BASE / 'public-host/Testing/Temporary/LastTest.log'),
                   cpu / 'LastTest.log')
    h.copy_checked(h.identity(BASE / 'public-host/CMakeCache.txt'),
                   cpu / 'CMakeCache.txt')
    for name in ['unsupported-interpreter.json', 'unsupported-configure.json']:
        h.copy_checked(h.identity(BASE / name), cpu / name)
    shutil.copytree(BASE / 'unsupported-lease', cpu / 'unsupported-lease')
    h.require('100% tests passed out of 22' in
              (cpu / 'stage3-host-ctest.log').read_text(), 'public host coverage')
    log = (cpu / 'LastTest.log').read_text()
    h.require('Ran 30 tests' in log and log.count('Ran 5 tests') >= 2 and
              '102 tests, 102 passed, 0 failed, 0 skipped' in log,
              'guard/rollback/supervisor/host contracts')
    h.require(not re.search(r'warning:|error:',
              (cpu / 'stage3-host-build.log').read_text()), 'zero-warning build')
    h.require('GPU guard Python: /usr/bin/python3' in
              (cpu / 'stage3-host-configure.log').read_text(), 'guard selection')
    rejected = h.read(cpu / 'unsupported-interpreter.json')
    h.require(rejected['returncode'] == 75 and not rejected['child_marker_exists']
              and rejected['outcome']['status'] == 'ENV_ABORT' and
              rejected['outcome']['child_returncode'] is None,
              'unsupported interpreter must not launch a child')
    h.require(h.read(cpu / 'unsupported-configure.json')['returncode'] != 0 and
              'GPU guard requires working pidfd APIs' in
              (cpu / 'stage3-unsupported-configure.log').read_text(),
              'explicit unsupported CMake interpreter')
    tools = [Path(__file__), Path(h.__file__), Path(h.build.__file__),
        ROOT / 'tools/evalscope/gpu_exclusive.py',
        ROOT / 'tools/evalscope/test_gpu_exclusive.py',
        ROOT / 'tools/evalscope/nvml_process_query.py',
        ROOT / 'tools/verify/audit_p2/rollback.py',
        ROOT / 'tools/verify/audit_p2/test_rollback.py',
        ROOT / 'tools/release/release.py', ROOT / 'tools/release/test_supervisor.py',
        ROOT / 'tests/host/CMakeLists.txt',
        ROOT / 'docs/AUDIT_P2_STAGE3_PLAN_2026-10-31.md']
    decision = dict(status='ACCEPT_TOOL_REPAIR', fixed=[f'A{i}' for i in range(1, 9)],
        binary_sha256=prior['binary_sha256'], previous_sidecar=prior['sidecar_sha256'],
        runtime_source_commit=old['source_commit'], history=history,
        tool_source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'],
                                                   cwd=ROOT, text=True).strip(),
        tools={str(p): h.sha(p) for p in tools}, cpu_seal=h.seal(cpu),
        public_ctest=22, guard_contracts=30, rollback_contracts=5,
        new_model_starts=0, new_generation_http=0, performance_acceptance=False,
        expected_capability_env_abort=1)
    h.save(BASE / 'decision.json', decision)
    PACKAGE.mkdir()
    for version, source in [('previous', PRIOR / 'delivery-package/previous'),
                            ('candidate', PRIOR / 'delivery-package/candidate')]:
        target = PACKAGE / version
        target.mkdir()
        for name in ['q4t', 'CMakeCache.txt', 'runtime-source.json']:
            h.copy_checked(h.identity(source / name), target / name)
        if version == 'previous':
            h.copy_checked(h.identity(source / 'q4t.release.json'),
                           target / 'q4t.release.json')
    h.copy_checked(h.identity(ROOT / 'build/q4t.release.json'),
                   PACKAGE / 'before-metadata.json')
    h.copy_checked(h.identity(BASE / 'decision.json'), PACKAGE / 'decision.json')
    for path in tools:
        target = PACKAGE / 'tools' / path.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        h.copy_checked(h.identity(path), target)
    metadata = copy.deepcopy(old)
    metadata.update(version='audit-p2-complete-20261031',
        audit_p2=dict(fixed=decision['fixed'], pending=[]),
        tool_source_commit=decision['tool_source_commit'],
        tool_acceptance=str(PACKAGE / 'decision.json'),
        tool_acceptance_sha256=h.sha(PACKAGE / 'decision.json'),
        previous_binary=str(PACKAGE / 'previous/q4t'),
        previous_release=str(PACKAGE / 'previous/q4t.release.json'))
    h.save(PACKAGE / 'candidate/q4t.release.json', metadata)
    h.save(BASE / 'package-receipt.json', dict(seal=h.seal(PACKAGE)))


def verify():
    h.verify_seal(PACKAGE)
    decision = h.read(PACKAGE / 'decision.json')
    h.require(h.verify_seal(BASE / 'cpu') == decision['cpu_seal'], 'CPU evidence')
    for path, digest in decision['tools'].items():
        h.require(h.sha(path) == digest, 'tool changed: ' + path)
    verify_runtime()
    h.require(h.sha(ROOT / 'build/q4t') == decision['binary_sha256'],
              'accepted binary changed')
    return decision


def install():
    h.require_guard(OUTPUT)
    decision = verify()
    h.require(not MARKER.exists() and h.sha(ROOT / 'build/q4t.release.json') ==
              decision['previous_sidecar'], 'metadata or recovery changed')
    h.save(MARKER, dict(package=str(PACKAGE)))
    h.fsync_directory(MARKER.parent)
    state = dict(status='INSTALLING_METADATA', started=time.time())
    def interrupted(*_):
        raise KeyboardInterrupt('metadata installation interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    try:
        h.replace_checked(PACKAGE / 'candidate/q4t.release.json',
                          ROOT / 'build/q4t.release.json', decision['previous_sidecar'])
        h.verify_pair(ROOT / 'build/q4t', ROOT / 'build/q4t.release.json',
                      decision['binary_sha256'])
        state['status'] = 'METADATA_INSTALLED_PENDING_GUARD'
    except BaseException:
        current = h.sha(ROOT / 'build/q4t.release.json')
        if current == h.sha(PACKAGE / 'candidate/q4t.release.json'):
            h.replace_checked(PACKAGE / 'before-metadata.json',
                              ROOT / 'build/q4t.release.json', current)
        h.require(h.sha(ROOT / 'build/q4t.release.json') == decision['previous_sidecar'],
                  'unknown metadata during restore')
        MARKER.unlink()
        h.fsync_directory(MARKER.parent)
        state['status'] = 'FAILED_METADATA_RESTORED'
        raise
    finally:
        state['finished'] = time.time()
        h.save(OUTPUT / 'install.json', state)


def finalize():
    decision = verify()
    guard = h.read(OUTPUT / 'lease/exit.json')
    h.require(guard['status'] == 'COMPLETED' and guard['child_returncode'] == 0,
              'metadata guard did not complete')
    h.require(h.read(OUTPUT / 'install.json')['status'] ==
              'METADATA_INSTALLED_PENDING_GUARD', 'metadata install did not complete')
    h.require(h.sha(ROOT / 'build/q4t.release.json') ==
              h.sha(PACKAGE / 'candidate/q4t.release.json'), 'metadata identity')
    h.require(h.read(MARKER)['package'] == str(PACKAGE), 'owned marker')
    MARKER.unlink()
    h.fsync_directory(MARKER.parent)
    h.save(OUTPUT / 'finalized.json', dict(status='DELIVERED_ALL_EIGHT',
        binary_sha256=decision['binary_sha256'],
        sidecar_sha256=h.sha(ROOT / 'build/q4t.release.json'),
        runtime_source_commit=decision['runtime_source_commit'],
        tool_source_commit=decision['tool_source_commit'],
        generation_http=40, model_starts=20, gpu_contracts=15,
        retained_installation_env_abort=2, persistent_service=False,
        finished=time.time()))
    archive = BASE / 'metadata-archive'
    shutil.copytree(OUTPUT, archive)
    h.save(BASE / 'metadata-archive-receipt.json', dict(seal=h.seal(archive)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'install', 'finalize'])
    globals()[parser.parse_args().action]()

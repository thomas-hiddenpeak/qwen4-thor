"""Freeze the authorized monitor-repair rerun without collecting or testing.

The failed parent remains immutable. Only named monitor/lifecycle dependencies
and repository documentation may differ; all collector/fixture/model contracts
and budgets are inherited unchanged. Native coverage may be zero when explicit.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
BASE = ROOT / '.q4t-work/suffix-nsys-rerun-20261024'
STAGE = BASE / 'diagnostic-v1'
PRIOR = ROOT / '.q4t-work/suffix-nsys-20261024'
PRIOR_STAGE = PRIOR / 'diagnostic-v1'
PARENT_COMMIT = '4e01cace4ad05d84020cf3d80d5d375399119c19'
PLAN_SHA = 'a995b1e6526da86dddaaca9870c1bebe875bc3f69666bf2ae1a31c0dd7d41a8a'
AUDIT_SHA = '92f57626fce89717f30be59d1162ea2e33dfe7705862208c8154413e9e0c52ed'
BINARY_SHA = 'f0ff0fe8e11791d561bc49f4dc42762415d4ff000836bbd8c38548723906c914'
FIXTURE_SHA = 'ad0241d7c1e6dff1a7b0feccf04815ea6d38d246d0112a41c39c00e5e3d4b1f4'
RUNTIME_SOURCE = '55363f18dedb947ac52e69e10475d3082cb71a82'
MONITOR = ROOT / 'tools/mtp_suffix_zero_hit/telemetry.py'
PENDING_TEST = ROOT / 'tools/mtp_suffix_zero_hit/test_exit_pending.py'
NATIVE = HERE / 'reproduce_exit_pending.py'
RUNNER = HERE / 'run_stage.py'
EXECUTABLE_CHANGES = {
    MONITOR: 'owned_exit_pending_monitor_repair',
    RUNNER: 'pending_empty_stage_and_final_flush_barrier',
    HERE / 'test_run_stage.py': 'pending_barrier_contract_tests',
}
NEW_TOOLS = {Path(__file__).resolve(), PENDING_TEST, NATIVE}
DOC = ROOT / 'docs/MTP_SUFFIX_NSYS_RERUN_2026-10-24.md'


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    with Path(path).open('x') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def documentation(path):
    return ((path.is_relative_to(ROOT / 'docs') and path.suffix == '.md') or
            path == HERE / 'README.md')


def check_parent():
    plan_path = PRIOR_STAGE / 'frozen-plan.json'
    audit_path = PRIOR / 'audit-stage.json'
    require(sha(plan_path) == PLAN_SHA and sha(audit_path) == AUDIT_SHA,
            'parent plan/integrity audit changed')
    plan, audit = read(plan_path), read(audit_path)
    state = read(PRIOR_STAGE / 'run/state.json')
    postflight = read(PRIOR_STAGE / 'postflight.json')
    outer = read(PRIOR_STAGE / 'lease/exit.json')
    events = [json.loads(line) for line in
              (PRIOR_STAGE / 'lease/events.jsonl').read_text().splitlines()
              if line.strip()]
    require(audit['evidence_integrity'] == 'PASS' and
            audit['evidence_validity'] == 'ENV_ABORT' and
            audit['measurement_complete'] is False and not audit['errors'] and
            audit['identities']['plan_sha256'] == PLAN_SHA and
            state['plan_sha256'] == PLAN_SHA and state['status'] == 'ENV_ABORT' and
            postflight['status'] == 'ENV_ABORT' and
            state['requests_completed'] == state['loads_started'] == 0 and
            not state['completed'] and 'finished' in state and
            outer['status'] == 'COMMAND_FAILED' and outer['child_returncode'] == 1 and
            events and events[-1]['event'] == 'lease_released',
            'parent ENV_ABORT/zero-model terminal records do not close')
    seal_path = PRIOR_STAGE / 'run/files.json'
    require(sha(seal_path) == audit['identities']['raw_seal_sha256'],
            'parent raw seal changed')
    seal = read(seal_path)
    actual = {str(p.relative_to(PRIOR_STAGE / 'run'))
              for p in (PRIOR_STAGE / 'run').rglob('*')
              if p.is_file() and p.name != 'files.json'}
    require(set(seal) == actual, 'parent raw inventory changed')
    for name, expected in seal.items():
        relative = Path(name)
        require(not relative.is_absolute() and '..' not in relative.parts and
                sha(PRIOR_STAGE / 'run' / relative) == expected,
                'parent raw changed: ' + name)
    snapshot = Path(plan['source_snapshot'])
    sources = {Path(name).relative_to(ROOT): expected
               for name, expected in plan['files'].items()
               if Path(name).is_relative_to(ROOT) and
               '.q4t-work' not in Path(name).relative_to(ROOT).parts}
    present = {p.relative_to(snapshot) for p in snapshot.rglob('*') if p.is_file()}
    require(present == set(sources) | {Path('worktree.patch')},
            'parent source snapshot inventory changed')
    for name, expected in sources.items():
        require(sha(snapshot / name) == expected,
                'parent frozen source changed: ' + str(name))
    require(postflight['state_sha256'] == sha(PRIOR_STAGE / 'run/state.json'),
            'parent postflight state identity changed')
    return plan, dict(
        outcome='ENV_ABORT_UNCHANGED', pooled_samples=False,
        source_commit=PARENT_COMMIT, plan_path=str(plan_path), plan_sha256=PLAN_SHA,
        audit_path=str(audit_path), audit_sha256=AUDIT_SHA,
        raw_seal_path=str(seal_path), raw_seal_sha256=sha(seal_path),
        source_snapshot=str(snapshot),
        source_snapshot_patch_sha256=sha(snapshot / 'worktree.patch'),
        postflight_sha256=sha(PRIOR_STAGE / 'postflight.json'),
        lease_exit_sha256=sha(PRIOR_STAGE / 'lease/exit.json'),
        previous_stage_accounting=dict(http_requests=0, model_loads=0,
            elapsed_seconds=state['elapsed_seconds']),
        scope='Separately authorized new stage; no parent response, fixture run, '
              'or performance sample is pooled or restored as environment-valid.')


def checked_cpu_proof():
    path = BASE / 'cpu-proof.json'
    proof = read(path)
    require(not proof['failed'] and not proof['errors'] and not proof['skipped'] and
            0 < proof['tests'] <= 100 and 0 < proof['suite_runs'] <= 2 and
            0 <= proof['elapsed_seconds'] <= 240,
            'new bounded CPU proof failed')
    required = {str(p.resolve()) for p in HERE.glob('*.py')}
    required |= {str(MONITOR), str(PENDING_TEST), str(NATIVE),
                 str(ROOT / 'tools/mtp_suffix_zero_hit/test_telemetry.py'),
                 str(BASE / 'cpu-tests.log')}
    require(required <= proof['files'].keys(),
            'CPU proof omits monitor, tools, tests or test log')
    for name, expected in proof['files'].items():
        require(Path(name).is_absolute() and sha(Path(name)) == expected,
                'CPU verified dependency changed: ' + name)
    return path, proof


def checked_native_proof():
    path = BASE / 'exit-pending-native.json'
    proof = read(path)
    rows_path = Path(proof['rows'])
    require(proof['passed'] is True and
            proof['completed_children'] == proof['children_started'] ==
            proof['max_children'] == 64 and proof['timeout_seconds'] == 60 and
            proof['child_counts_by_threads'] == {'1': 32, '8': 32} and
            0 <= proof['elapsed_seconds'] <= 60 and
            proof['cleanup_complete'] is True and proof['all_owned_reaped'] is True and
            type(proof['coverage_reproduced']) is bool and
            not proof['failures'] and
            proof['tool_sha256'] == sha(NATIVE) and
            proof['monitor_sha256'] == sha(MONITOR),
            'native CPU proof lacks bounded completion/cleanup/identity/coverage')
    require(rows_path == BASE / 'exit-pending-native-rows.jsonl' and
            sha(rows_path) == proof['rows_sha256'],
            'native CPU raw rows identity changed')
    return path, proof, rows_path


def checked_kernel_contract():
    path = BASE / 'references/kernel-contract.json'
    contract = read(path)
    release = os.uname().release
    original = Path(contract['original_path'])
    requested = Path(contract['requested_path'])
    archived = Path(contract['copy_path'])
    require(contract['schema'] == 1 and contract['release'] == release and
            type(contract['PF_EXITING']) is int and contract['PF_EXITING'] == 4 and
            original.is_absolute() and original.resolve() == original and
            requested == Path('/lib/modules') / release / 'build/include/linux/sched.h' and
            requested.resolve() == original and
            archived == BASE / 'references' / ('local-' + release + '-sched.h') and
            archived.resolve() != original and
            sha(original) == contract['sha256'] == contract['copy_sha256'] == sha(archived),
            'running-kernel header contract or independent copy identity changed')
    definitions = re.findall(
        r'^#define[ \t]+PF_EXITING[ \t]+(0[xX][0-9a-fA-F]+|[0-9]+)'
        r'[ \t]*(?:/\*.*\*/)?[ \t]*$', original.read_text(), re.MULTILINE)
    require(len(definitions) == 1 and int(definitions[0], 0) == 4,
            'matching kernel header does not define PF_EXITING as 4')
    return path, contract, {path, original, requested, archived}


def check_dependencies(old):
    differences = []
    for name, before in old['files'].items():
        path = Path(name)
        after = sha(path)
        if after != before:
            role = EXECUTABLE_CHANGES.get(path)
            if role is None and documentation(path):
                role = 'documentation'
            require(role is not None, 'unapproved inherited dependency change: ' + name)
            differences.append(dict(path=name, before=before, after=after, role=role))
    changed = {Path(row['path']) for row in differences}
    require(MONITOR in changed and RUNNER in changed,
            'monitor repair and stage barrier must both be bound as changes')
    current = {ROOT / 'CMakeLists.txt'}
    for name in ('src', 'include', 'cmake'):
        current |= {p for p in (ROOT / name).rglob('*') if p.is_file()}
    prior = {Path(p) for p in old['files'] if Path(p) == ROOT / 'CMakeLists.txt' or
             any(Path(p).is_relative_to(ROOT / d) for d in ('src', 'include', 'cmake'))}
    require(current == prior, 'runtime source/header/CMake inventory changed')
    require(old['candidate_sha256'] == sha(Path(old['candidate'])) == BINARY_SHA and
            old['candidate_source_commit'] == RUNTIME_SOURCE and
            old['fixture_binary_sha256'] == sha(Path(old['fixture_binary'])) == FIXTURE_SHA,
            'reused model or fixture binary identity changed')
    for name, expected in old['reuse_review']['content']['files'].items():
        require(sha(ROOT / name) == expected, 'reviewed runtime source changed: ' + name)
    for name, expected in old['model_file_metadata'].items():
        info = Path(name).stat()
        require(dict(size=info.st_size, mtime_ns=info.st_mtime_ns,
                     device=info.st_dev, inode=info.st_ino) == expected,
                'model metadata changed: ' + name)
    for path in NEW_TOOLS:
        require(str(path) not in old['files'] and path.is_file(),
                'expected new tool missing or unexpectedly inherited: ' + str(path))
    current_python = {p.resolve() for p in HERE.glob('*.py')}
    prior_python = {Path(p) for p in old['files']
                    if Path(p).parent == HERE and Path(p).suffix == '.py'}
    require(current_python == prior_python | {p for p in NEW_TOOLS if p.parent == HERE},
            'unexpected new collector Python dependency')
    return differences


def main():
    require(not STAGE.exists(), 'refuse existing rerun stage')
    old, prior_abort = check_parent()
    source_commit = subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    require(subprocess.run(['git', 'merge-base', '--is-ancestor', PARENT_COMMIT,
                           source_commit], cwd=ROOT, check=False).returncode == 0,
            'current branch does not contain authorized parent commit')
    differences = check_dependencies(old)
    cpu_path, cpu = checked_cpu_proof()
    native_path, native, native_rows = checked_native_proof()
    kernel_path, kernel, kernel_paths = checked_kernel_contract()
    require(DOC.is_file(), 'new frozen-stage document missing')
    require((old['http_requests'], old['http_loads'], old['stage_timeout_seconds'],
             old['direct_contract_timeout_seconds'], len(old['steps'])) ==
            (6, 4, 5400, 1200, 5), 'parent fixed scope differs')

    STAGE.mkdir(parents=True)
    fixtures = STAGE / 'fixtures'
    fixtures.mkdir()
    plan = copy.deepcopy(old)
    plan.update(version=4,
        classification='monitor exit-pending repair and bounded collector rerun; '
                       'no runtime optimization or performance delivery',
        source_commit=source_commit, output=str(STAGE / 'run'),
        lease=str(STAGE / 'lease'), source_snapshot=str(STAGE / 'source-snapshot'),
        files={}, prior_abort=prior_abort,
        prior_cpu_proof=old['cpu_proof'],
        prior_monitor_fix=old.get('monitor_fix'),
        cpu_proof=dict(path=str(cpu_path), sha256=sha(cpu_path),
                       tests=cpu['tests'], suite_runs=cpu['suite_runs']),
        monitor_fix=dict(classification='bounded_owned_exit_pending',
            cpu_proof=dict(path=str(cpu_path), sha256=sha(cpu_path)),
            native_proof=dict(path=str(native_path), sha256=sha(native_path)),
            kernel_contract=dict(path=str(kernel_path), sha256=sha(kernel_path),
                                 release=kernel['release'], PF_EXITING=kernel['PF_EXITING']),
            monitor_sha256=sha(MONITOR), runner_sha256=sha(RUNNER),
            exit_pending=dict(ttl_seconds=5, capacity=16, poll_timeout_ms=0),
            scope='Policy metadata only; exact constants are bound by source SHA. '
                  'Pending is not terminal or valid resource data. New stage '
                  'transitions and final barriers require pending count zero.'),
        native_monitor_proof=dict(path=str(native_path), sha256=sha(native_path),
            completed_children=native['completed_children'],
            coverage_reproduced=native['coverage_reproduced'],
            scope='Native CPU-only coverage; zero reproduction is explicit and '
                  'does not claim native coverage of the pending branch.'),
        monitor_repair_dependency_changes=differences,
        new_stage_cpu_budget=dict(max_test_methods=100, max_suite_runs=2,
                                 max_elapsed_seconds=240),
        new_stage_native_budget=dict(max_children=64, max_elapsed_seconds=60),
        first_test='Targeted monitor CPU/native validation precedes the same '
                   'four collector controls; prior normal HTTP and repeat '
                   'evidence is reused only within its unchanged scope.',
        failed_parent_samples_reused=False)
    copied = {}
    for previous, step in zip(old['steps'], plan['steps']):
        for case in step['cases']:
            source = Path(case['path'])
            target = fixtures / source.name
            if target in copied:
                require(copied[target] == sha(source), 'fixture basename collision')
            else:
                shutil.copy2(source, target)
                copied[target] = sha(source)
            require(sha(target) == copied[target], 'fixture copy identity differs')
            case['path'] = str(target)
        step['command'] = [sys.executable, '-B', str(RUNNER), '--plan',
                           str(STAGE / 'frozen-plan.json'), '--custom-step', step['name']]
        restored = copy.deepcopy(step)
        restored['command'] = previous['command']
        for before, after in zip(previous['cases'], restored['cases']):
            after['path'] = before['path']
        require(restored == previous, 'matrix/configuration changed during rerun prepare')

    paths = set(map(Path, old['files'])) | set(copied) | NEW_TOOLS | kernel_paths
    paths |= {cpu_path, native_path, native_rows, DOC, PRIOR_STAGE / 'frozen-plan.json',
              PRIOR_STAGE / 'run/files.json', PRIOR_STAGE / 'postflight.json',
              PRIOR_STAGE / 'lease/exit.json', PRIOR_STAGE / 'lease/events.jsonl',
              PRIOR / 'audit-stage.json', PRIOR / 'audit-stage.md',
              PRIOR / 'result-analysis.json', PRIOR / 'result-analysis.md',
              PRIOR / 'collector-result-review.json', PRIOR / 'collector-result-review.md',
              PRIOR / 'monitor-exit-forensics.json'}
    paths |= set(map(Path, cpu['files']))
    paths |= {p for p in BASE.rglob('*') if p.is_file() and
              not p.is_relative_to(STAGE) and '__pycache__' not in p.parts}
    paths |= {p for p in HERE.rglob('*') if p.is_file() and '__pycache__' not in p.parts}
    snapshot = Path(plan['source_snapshot'])
    snapshot.mkdir()
    for path in sorted(paths):
        plan['files'][str(path)] = sha(path)
        if path.is_relative_to(ROOT) and '.q4t-work' not in path.relative_to(ROOT).parts:
            target = snapshot / path.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            require(sha(target) == plan['files'][str(path)], 'source copy changed')
    (snapshot / 'worktree.patch').write_bytes(subprocess.check_output(
        ['git', 'diff', 'HEAD'], cwd=ROOT))
    save(STAGE / 'frozen-plan.json', plan)
    print(json.dumps(dict(plan=str(STAGE / 'frozen-plan.json'),
        sha256=sha(STAGE / 'frozen-plan.json'), frozen_files=len(plan['files']),
        http_requests=6, model_starts=4, fixture_captures=4,
        conditional_model_captures=4, seconds=5400,
        native_coverage_reproduced=native['coverage_reproduced']), indent=2))


if __name__ == '__main__':
    main()

"""Freeze one separately authorized rerun after the resource-monitor repair.

No inference, HTTP, GPU, tests, live process inspection or sensor sampling.
The unchanged observation binary and 49-request matrix come from the sealed
ENV_ABORT attempt. That attempt contributes no samples to this new stage.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
OLD_BASE = ROOT / '.q4t-work/suffix-execution-20261024'
OLD_STAGE = OLD_BASE / 'diagnostic-v1'
NEW_BASE = ROOT / '.q4t-work/suffix-execution-rerun-20261024'
PLAN_SHA = 'a145dafe688ef60ec117d76fb5b3d6116a39b22bf92c412aede811c45e5df47b'
AUDIT_SHA = 'a260c7037deeba0a4779da4c4ec6f36e36a1a6da70ceed623931a27df013cef5'
BINARY_SHA = 'f0ff0fe8e11791d561bc49f4dc42762415d4ff000836bbd8c38548723906c914'
RUNTIME_SOURCE = '55363f18dedb947ac52e69e10475d3082cb71a82'
MONITOR = ROOT / 'tools/mtp_suffix_zero_hit/telemetry.py'
MONITOR_TEST = ROOT / 'tools/mtp_suffix_zero_hit/test_telemetry.py'
sys.path.insert(0, str(ROOT / 'tools/mtp_suffix_zero_hit'))
import telemetry
sys.path.insert(0, str(Path(__file__).resolve().parent))
from spec import validate


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read(path):
    return json.loads(path.read_text())


def save(path, value):
    with path.open('x') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def documentation(path):
    return (path.is_relative_to(ROOT / 'docs') and path.suffix == '.md') or (
        path.is_relative_to(ROOT) and '.q4t-work' not in path.relative_to(ROOT).parts
        and path.name in ('README.md', 'README-rerun.md'))


def check_prior(plan):
    state = read(OLD_STAGE / 'run/state.json')
    outer = read(OLD_STAGE / 'lease/exit.json')
    events = [json.loads(line) for line in
              (OLD_STAGE / 'lease/events.jsonl').read_text().splitlines() if line.strip()]
    audit = read(OLD_BASE / 'audit-execution-v2.json')
    if (sha(OLD_STAGE / 'frozen-plan.json') != PLAN_SHA or
            sha(OLD_BASE / 'audit-execution-v2.json') != AUDIT_SHA or
            state['plan_sha256'] != PLAN_SHA or state['status'] != 'ENV_ABORT' or
            'finished' not in state or outer['status'] != 'COMMAND_FAILED' or
            outer['child_returncode'] != 1 or not events or
            events[-1]['event'] != 'lease_released' or
            audit['evidence_validity'] != 'ENV_ABORT' or audit['evidence_integrity'] != 'PASS' or
            audit['measurement_complete'] is not False or audit['errors'] or
            audit['identities']['plan_sha256'] != PLAN_SHA or
            audit['identities']['raw_seal_sha256'] != sha(OLD_STAGE / 'run/files.json')):
        raise RuntimeError('previous ENV_ABORT and its immutable integrity audit do not close')
    for name, expected_hash in audit['environment']['hashes'].items():
        if sha(OLD_STAGE / name) != expected_hash:
            raise RuntimeError('previous guard/postflight evidence changed: ' + name)
    raw = read(OLD_STAGE / 'run/files.json')
    actual = {str(p.relative_to(OLD_STAGE / 'run')) for p in (OLD_STAGE / 'run').rglob('*')
              if p.is_file() and p.name != 'files.json'}
    if set(raw) != actual:
        raise RuntimeError('previous raw evidence inventory changed')
    for name, expected in raw.items():
        path = Path(name)
        if path.is_absolute() or '..' in path.parts or sha(OLD_STAGE / 'run' / path) != expected:
            raise RuntimeError('previous raw evidence changed: ' + name)
    snapshot = Path(plan['source_snapshot'])
    expected = {str(Path(name).relative_to(ROOT)) for name in plan['files']
                if Path(name).is_relative_to(ROOT) and
                '.q4t-work' not in Path(name).relative_to(ROOT).parts}
    actual = {str(p.relative_to(snapshot)) for p in snapshot.rglob('*') if p.is_file()}
    if actual != expected | {'worktree.patch'}:
        raise RuntimeError('previous source snapshot inventory changed')
    for name in expected:
        if sha(snapshot / name) != plan['files'][str(ROOT / name)]:
            raise RuntimeError('previous source snapshot changed: ' + name)
    if sha(snapshot / 'worktree.patch') != audit['identities']['source_snapshot_patch_sha256']:
        raise RuntimeError('previous snapshot patch changed')
    return dict(outcome='ENV_ABORT_UNCHANGED', pooled_samples=False,
        stage=str(OLD_STAGE), plan_sha256=PLAN_SHA,
        audit=str(OLD_BASE / 'audit-execution-v2.json'),
        audit_sha256=sha(OLD_BASE / 'audit-execution-v2.json'),
        raw_seal=str(OLD_STAGE / 'run/files.json'), raw_seal_sha256=sha(OLD_STAGE / 'run/files.json'),
        previous_stage_accounting=dict(requests_completed=state['requests_completed'],
            loads_started=state['loads_started'], elapsed_seconds=state['elapsed_seconds']),
        raw_successful_responses=audit['requests']['stored_successful_responses'],
        scope='New user-authorized stage, not a continuation or restoration of the failed environment; no previous response/performance sample is pooled.')


def checked_monitor_proof(path, log):
    proof = read(path)
    if (proof['tool_sha256'] != sha(MONITOR) or proof['test_sha256'] != sha(MONITOR_TEST) or
            proof['test_log_sha256'] != sha(log) or proof['tests_passed'] < 11 or
            proof['tests_skipped'] != 0 or proof['tests_failed'] != 0 or proof['tests_errors'] != 0 or
            proof['new_http_or_gpu'] != 0 or proof['real_owned_children_reaped'] is not True or
            proof['bounded_exit_confirmation'] != dict(per_task_ms=50, per_sample_ms=100)):
        raise RuntimeError('new monitor CPU proof does not bind passed bounded repair checks')
    reproduction = proof['reproduction']
    source = Path(reproduction['path'])
    if (not source.is_absolute() or sha(source) != reproduction['sha256'] or
            not isinstance(reproduction['status'], str) or not reproduction['status'] or
            type(reproduction['old_monitor_failure_reproduced']) is not bool or
            type(reproduction['nonterminal_error_observed']) is not bool):
        raise RuntimeError('CPU reproduction identity or explicit outcome is missing')
    return proof, source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', type=Path, default=NEW_BASE / 'diagnostic-v1')
    parser.add_argument('--monitor-proof', type=Path, required=True)
    parser.add_argument('--monitor-test-log', type=Path, required=True)
    parser.add_argument('--extra-file', type=Path, action='append', default=[])
    args = parser.parse_args()
    stage, proof_path, test_log = args.stage.resolve(), args.monitor_proof.resolve(), args.monitor_test_log.resolve()
    if not stage.is_relative_to(NEW_BASE) or stage.exists():
        parser.error('use a new stage within the authorized rerun base')
    old = read(OLD_STAGE / 'frozen-plan.json')
    validate(old)
    prior_abort = check_prior(old)
    if (sha(Path(old['candidate'])) != BINARY_SHA or old['candidate_sha256'] != BINARY_SHA or
            old['candidate_source_commit'] != RUNTIME_SOURCE):
        raise RuntimeError('observation binary/runtime source changed; rebuild is not authorized by this plan')
    review = read(Path(old['reuse_review']['path']))
    if (sha(Path(old['reuse_review']['path'])) != old['reuse_review']['sha256'] or
            review != old['reuse_review']['content'] or review['source_commit'] != RUNTIME_SOURCE):
        raise RuntimeError('original observation-only source review changed')
    for name, expected in review['files'].items():
        if sha(ROOT / name) != expected:
            raise RuntimeError('observation runtime changed: ' + name)
    proof, reproduction_path = checked_monitor_proof(proof_path, test_log)
    differences = []
    for name, before in old['files'].items():
        path = Path(name)
        after = sha(path)
        if after == before:
            continue
        kind = 'monitor_repair' if path in (MONITOR, MONITOR_TEST) else (
            'documentation' if documentation(path) else None)
        if kind is None:
            raise RuntimeError('unapproved executable/dependency identity change: ' + name)
        differences.append(dict(path=name, before=before, after=after, role=kind))
    if {row['path'] for row in differences if row['role'] == 'monitor_repair'} != {
            str(MONITOR), str(MONITOR_TEST)}:
        raise RuntimeError('both repaired monitor and its affected test must have new bound identities')
    runtime_paths = {ROOT / 'CMakeLists.txt'}
    for directory in ('src', 'include', 'cmake'):
        runtime_paths |= {p for p in (ROOT / directory).rglob('*') if p.is_file()}
    old_runtime_paths = {Path(p) for p in old['files'] if
        Path(p) == ROOT / 'CMakeLists.txt' or any(Path(p).is_relative_to(ROOT / directory)
                                               for directory in ('src', 'include', 'cmake'))}
    if runtime_paths != old_runtime_paths:
        raise RuntimeError('runtime source/header/CMake inventory changed')
    capability_path = Path(old['nsys']['capability_path'])
    if (sha(capability_path) != old['nsys']['capability_sha256'] or
            read(capability_path) != old['nsys']['capabilities'] or
            sha(Path(old['nsys']['path'])) != old['nsys']['sha256']):
        raise RuntimeError('Nsight identity/capabilities changed')

    stage.mkdir(parents=True)
    fixtures = stage / 'fixtures'
    fixtures.mkdir()
    plan = copy.deepcopy(old)
    plan.update(version=2, classification='bounded observation rerun after monitor repair; no optimization delivery',
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'],cwd=ROOT,text=True).strip(),
        output=str(stage / 'run'), lease=str(stage / 'lease'), source_snapshot=str(stage / 'source-snapshot'),
        files={}, prior_abort=prior_abort, prior_monitor_fix=old['monitor_fix'],
        monitor_fix=dict(proof, proof_path=str(proof_path), proof_sha256=sha(proof_path),
                         test_log_path=str(test_log), scope='New CPU repair proof only; true GPU-stage lifecycle still requires this whole stage to finish.'),
        monitor_repair_dependency_changes=differences,
        first_test='First inference/GPU test is normal real evalscope HTTP; separately authorized targeted CPU monitor repair checks are frozen prerequisites.',
        build_reuse=dict(new_builds=0, binary_sha256=BINARY_SHA, runtime_source=RUNTIME_SOURCE,
                         scope='Existing zero-warning observation build reused exactly; no new runtime candidate.'),
        offline_analysis='Offline analyzers are sealed separately before post-stage execution. They are not measurement dependencies, and raw evidence is immutable.')
    sources = telemetry.discover_sources()
    if sources != old['telemetry_sources']:
        raise RuntimeError('monitor source capability/path inventory changed; no automatic probe substitution')
    plan['telemetry_sources'] = sources
    copied = {}
    runner = ROOT / 'tools/mtp_suffix_execution/run_stage.py'
    for step in plan['steps']:
        for case in step['cases']:
            source = Path(case['path'])
            target = fixtures / source.name
            if source not in copied:
                shutil.copy2(source, target)
                copied[source] = target
            case['path'] = str(target)
        step['command'] = [sys.executable, '-B', str(runner), '--plan',
                           str(stage / 'frozen-plan.json'), '--custom-step', step['name']]
    unique = {}
    for step in plan['steps']:
        for case in step['cases']:
            if case['id'] not in unique:
                unique[case['id']] = dict(case, repeats=1)
    save(fixtures / 'manifest.json', list(unique.values()))
    validate(plan)
    # The step data except output paths/driver command must remain identical.
    for previous, current in zip(old['steps'], plan['steps']):
        restored = copy.deepcopy(current)
        restored['command'] = previous['command']
        for old_case, new_case in zip(previous['cases'], restored['cases']):
            new_case['path'] = old_case['path']
        if restored != previous:
            raise RuntimeError('case/order/hash/mode matrix changed during rerun preparation')
    paths = set(map(Path, old['files'])) | set(fixtures.glob('*'))
    paths |= {Path(__file__).resolve(), ROOT / 'tools/mtp_suffix_execution/README-rerun.md',
              proof_path, test_log, reproduction_path, OLD_STAGE / 'frozen-plan.json',
              OLD_STAGE / 'postflight.json', OLD_BASE / 'audit-execution-v2.json',
              OLD_BASE / 'audit-execution-v2.md'}
    paths |= {p for p in (OLD_STAGE / 'run').rglob('*') if p.is_file()}
    paths |= {p for p in (OLD_STAGE / 'lease').rglob('*') if p.is_file()}
    paths |= {p for p in Path(old['source_snapshot']).rglob('*') if p.is_file()}
    paths |= {p.resolve() for p in args.extra_file}
    snapshot = Path(plan['source_snapshot'])
    snapshot.mkdir()
    for path in sorted(paths):
        plan['files'][str(path)] = sha(path)
        if path.is_relative_to(ROOT) and '.q4t-work' not in path.relative_to(ROOT).parts:
            target = snapshot / path.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    (snapshot / 'worktree.patch').write_bytes(subprocess.check_output(['git','diff','HEAD'],cwd=ROOT))
    save(stage / 'frozen-plan.json', plan)
    print(json.dumps(dict(plan=str(stage/'frozen-plan.json'),sha256=sha(stage/'frozen-plan.json'),
        requests=49,starts=13,seconds=7200,candidate=BINARY_SHA,
        prior_abort='retained; zero pooled samples',monitor_proof=sha(proof_path)),indent=2))


if __name__ == '__main__':
    main()

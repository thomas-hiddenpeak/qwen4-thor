"""Freeze the bounded interactive-collector repair; never collect or test.

All new inputs are explicit. In particular, this script never recursively adds
its output BASE or preparation stdout to the frozen manifest. The previous
ENV_ABORT and its two local known-work controls keep their original status.
"""
import argparse
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
BASE = ROOT / '.q4t-work/suffix-delivery-20261026'
PRIOR = ROOT / '.q4t-work/suffix-nsys-rerun-20261024'
PRIOR_STAGE = PRIOR / 'diagnostic-v2'
ENTRY_COMMIT = 'd25a03069e2e74bf7a86ce6c3e73c52cd4ed1b8a'
PLAN_SHA = 'f6951b0c65b2b3918ca0d2696c14b8a724e0c5e10dae4321ac0ffdac8b7e536b'
AUDIT_SHA = 'cb34554cbdbcf5bacc2b68b8e526ddd9978e80c8ec7f7ef86bbaf3124799f7f3'
BINARY_SHA = 'f0ff0fe8e11791d561bc49f4dc42762415d4ff000836bbd8c38548723906c914'
FIXTURE_SHA = 'ad0241d7c1e6dff1a7b0feccf04815ea6d38d246d0112a41c39c00e5e3d4b1f4'
RUNTIME_SOURCE = '55363f18dedb947ac52e69e10475d3082cb71a82'
RUNNER = HERE / 'run_stage.py'
INTERACTIVE = HERE / 'interactive.py'
DOC = ROOT / 'docs/MTP_SUFFIX_DELIVERY_2026-10-26.md'
CHANGES = {RUNNER: 'interactive_collector_lifecycle_repair',
           HERE / 'test_run_stage.py': 'affected_runner_contracts'}
NEW_TOOLS = {Path(__file__).resolve(), INTERACTIVE, HERE / 'test_interactive.py'}
VARIANTS = [dict(name='direct-interactive', wrapper=False, external=True,
                 lifecycle='interactive'),
            dict(name='wrapper-interactive', wrapper=True, external=True,
                 lifecycle='interactive')]
REUSED_MODULES = {
    ROOT / 'tools/mtp_suffix_zero_hit/test_telemetry.py': 39,
    ROOT / 'tools/mtp_suffix_zero_hit/test_exit_pending.py': 25,
    HERE / 'test_fixture_audit.py': 14,
    HERE / 'test_analyze_gpu.py': 8,
}


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


def checked_manifest(values, base=None):
    paths = set()
    for name, expected in values.items():
        relative = Path(name)
        if base is None:
            require(relative.is_absolute(), 'absolute frozen input required')
            path = relative
        else:
            require(not relative.is_absolute() and '..' not in relative.parts,
                    'unsafe sealed relative path')
            path = base / relative
            require(not path.is_symlink(), 'raw evidence must not be a symlink')
        require(path.is_file() and sha(path) == expected,
                'frozen input changed: ' + str(path))
        paths.add(path)
    return paths


def check_parent():
    plan_path, audit_path = PRIOR_STAGE / 'frozen-plan.json', PRIOR / 'audit-rerun.json'
    require(sha(plan_path) == PLAN_SHA and sha(audit_path) == AUDIT_SHA,
            'parent plan or independent audit changed')
    plan, audit = read(plan_path), read(audit_path)
    state = read(PRIOR_STAGE / 'run/state.json')
    post = read(PRIOR_STAGE / 'postflight.json')
    outer = read(PRIOR_STAGE / 'lease/exit.json')
    events = [json.loads(line) for line in (PRIOR_STAGE / 'lease/events.jsonl').read_text().splitlines()]
    require(audit['evidence_integrity'] == 'PASS' and not audit['errors'] and
            audit['evidence_validity'] == post['status'] == 'ENV_ABORT' and
            audit['measurement_complete'] is False and
            audit['identities']['plan_sha256'] == state['plan_sha256'] == PLAN_SHA and
            state['status'] == 'STOPPED' and not state['completed'] and
            state['requests_completed'] == state['loads_started'] == 0 and
            outer['status'] == 'COMMAND_FAILED' and outer['child_returncode'] == 1 and
            events[-1]['event'] == 'lease_released',
            'parent stopped/ENV_ABORT/zero-model terminal records disagree')
    seal_path = PRIOR_STAGE / 'run/files.json'
    require(sha(seal_path) == audit['identities']['raw_seal_sha256'],
            'parent raw seal identity changed')
    seal = read(seal_path)
    present = {str(p.relative_to(PRIOR_STAGE / 'run')) for p in (PRIOR_STAGE / 'run').rglob('*')
               if p.is_file() and p.name != 'files.json'}
    require(set(seal) == present, 'parent raw inventory changed')
    paths = checked_manifest(seal, PRIOR_STAGE / 'run')
    snapshot = Path(plan['source_snapshot'])
    expected = {Path(p).relative_to(ROOT): digest for p, digest in plan['files'].items()
                if Path(p).is_relative_to(ROOT) and '.q4t-work' not in Path(p).relative_to(ROOT).parts}
    actual = {p.relative_to(snapshot) for p in snapshot.rglob('*') if p.is_file()}
    require(actual == set(expected) | {Path('worktree.patch')}, 'parent snapshot inventory changed')
    paths |= checked_manifest({str(snapshot / p): digest for p, digest in expected.items()})
    require(sha(snapshot / 'worktree.patch') == audit['identities']['source_snapshot']['worktree_patch_sha256'],
            'parent snapshot patch changed')
    paths |= {plan_path, audit_path, PRIOR / 'audit-rerun.md', seal_path,
              snapshot / 'worktree.patch', PRIOR_STAGE / 'run/state.json',
              PRIOR_STAGE / 'postflight.json', PRIOR_STAGE / 'lease/exit.json',
              PRIOR_STAGE / 'lease/events.jsonl'}
    results = read(PRIOR_STAGE / 'run/fixture-controls/results.json')
    require([r['name'] for r in results] == ['direct-stop', 'wrapper-stop'],
            'expected two historical local controls')
    reuse = {}
    for row in results:
        folder = PRIOR_STAGE / 'run/fixture-controls' / row['name']
        known = read(folder / 'known-work-audit.json')
        negative = read(folder / 'negative-controls.json')
        exited = read(folder / 'exit.json')
        require(known['fixture_contract_passed'] is True and known['known_work_complete'] is True and
                known['gpu_memory_coverage_complete'] is True and row['negative_controls_passed'] is True and
                set(negative['derivatives']) == {'drop-launch-kernel-pair', 'drop-last-nvtx'} and
                all(v['expected_rejected'] and not v['audit']['fixture_contract_passed']
                    for v in negative['derivatives'].values()) and
                exited['complete'] is True and exited['launcher'] == 0 and
                not exited['cleanup_errors'] and not exited['forced_termination'],
                'historical local known-work/negative/exit contract changed')
        require(row['diagnostics_valid'] is False, 'historical target warnings must remain explicit')
        reuse[row['name']] = dict(source_plan=str(plan_path), source_plan_sha256=PLAN_SHA,
            source_raw_seal=str(seal_path), source_raw_seal_sha256=sha(seal_path), output=str(folder),
            known_work_audit_sha256=sha(folder / 'known-work-audit.json'),
            negative_controls_sha256=sha(folder / 'negative-controls.json'),
            exit_sha256=sha(folder / 'exit.json'),
            profile_diagnostics_sha256=sha(folder / 'profile-diagnostics.json'),
            raw_hashes=row['raw_hashes'], source_stage_validity='ENV_ABORT',
            scope='historical_partial_known_work_only; no new sample or diagnostics pass')
    return plan, audit, reuse, paths


def check_dependencies(old):
    changes = []
    for name, before in old['files'].items():
        path, after = Path(name), sha(name)
        if before == after:
            continue
        role = CHANGES.get(path)
        if role is None and ((path.is_relative_to(ROOT / 'docs') and path.suffix in ('.md', '.json')) or
                             path == HERE / 'README.md'):
            role = 'documentation'
        require(role is not None, 'unapproved inherited input change: ' + name)
        changes.append(dict(path=name, before=before, after=after, role=role))
    require(RUNNER in {Path(r['path']) for r in changes}, 'interactive runner repair not bound')
    runtime_files = {ROOT / 'CMakeLists.txt'}
    for name in ('src', 'include', 'cmake'):
        runtime_files |= {p for p in (ROOT / name).rglob('*') if p.is_file()}
    inherited_runtime = {Path(p) for p in old['files'] if Path(p) == ROOT / 'CMakeLists.txt' or
                         any(Path(p).is_relative_to(ROOT / d) for d in ('src', 'include', 'cmake'))}
    require(runtime_files == inherited_runtime, 'unexpected runtime/header/build dependency inventory change')
    require(old['candidate_sha256'] == sha(old['candidate']) == BINARY_SHA and
            old['candidate_source_commit'] == RUNTIME_SOURCE and
            old['fixture_binary_sha256'] == sha(old['fixture_binary']) == FIXTURE_SHA,
            'observation/fixture binary or runtime identity changed')
    for name, digest in old['reuse_review']['content']['files'].items():
        require(sha(ROOT / name) == digest, 'reused runtime source changed: ' + name)
    for name, expected in old['model_file_metadata'].items():
        stat = Path(name).stat()
        require(dict(size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                     device=stat.st_dev, inode=stat.st_ino) == expected,
                'model metadata changed: ' + name)
    prior_python = {Path(p) for p in old['files'] if Path(p).parent == HERE and Path(p).suffix == '.py'}
    require({p.resolve() for p in HERE.glob('*.py')} == prior_python | NEW_TOOLS,
            'unexpected collector module; explicitly review dependency closure')
    return changes


def check_reused_cpu(old):
    path = Path(old['cpu_proof']['path']); proof = read(path)
    require(sha(path) == old['cpu_proof']['sha256'] and proof['tests'] == 97 and
            proof['suite_runs'] == 1 and proof['failed'] == proof['errors'] == proof['skipped'] == 0,
            'original CPU97 proof changed')
    snapshot = Path(old['source_snapshot'])
    paths = {path}
    for name, digest in proof['files'].items():
        source = Path(name)
        # The two affected runner sources are deliberately revalidated below.
        check = snapshot / source.relative_to(ROOT) if source in CHANGES else source
        require(sha(check) == digest, 'prior CPU proof or unaffected dependency changed: ' + name)
        paths.add(check)
    for module in REUSED_MODULES:
        require(sha(module) == proof['files'][str(module)], 'reused CPU module changed')
    native_path = Path(old['native_monitor_proof']['path']); native = read(native_path)
    require(sha(native_path) == old['native_monitor_proof']['sha256'] and native['passed'] is True and
            native['completed_children'] == native['children_started'] == 64 and
            native['cleanup_complete'] is True and native['all_owned_reaped'] is True and
            not native['failures'] and
            native['monitor_sha256'] == sha(ROOT / 'tools/mtp_suffix_zero_hit/telemetry.py') and
            native['tool_sha256'] == sha(HERE / 'reproduce_exit_pending.py') and
            sha(native['rows']) == native['rows_sha256'], 'unchanged native64 proof/dependencies required')
    paths |= {native_path, Path(native['rows'])}
    return dict(cpu97_proof=dict(path=str(path), sha256=sha(path)),
        reused_methods={str(k): v for k,v in REUSED_MODULES.items()}, reused_method_count=86,
        affected_old_runner_methods=11,
        native64=dict(path=str(native_path), sha256=sha(native_path),
            scope='CPU tight-loop only; no inherited collector pending-branch coverage'),
        scope='Only unchanged monitor64/fixture14/analyze8 contracts reused; old runner11 rerun with the new interactive contracts.'), paths


def check_cpu(path):
    proof = read(path)
    require(proof['failed'] == proof['errors'] == proof['skipped'] == 0 and
            11 < proof['tests'] <= 40 and 0 < proof['suite_runs'] <= 2 and
            0 < proof['elapsed_seconds'] <= 240, 'bounded affected CPU proof failed')
    if proof['suite_runs'] == 1:
        require(proof['elapsed_seconds'] <= 120, 'first affected CPU suite exceeds 120 seconds')
    else:
        runs = proof['suites']
        require(len(runs) == 2 and 0 < runs[0]['elapsed_seconds'] <= 120 and
                sum(r['elapsed_seconds'] for r in runs) <= 240 and
                bool(proof.get('specific_failure_fix')), 'second CPU suite lacks fixed failure/budget receipt')
    required = {str(p.resolve()) for p in HERE.glob('*.py')}
    required |= {str(ROOT / 'tools/mtp_suffix_zero_hit/telemetry.py'), str(path.parent / 'cpu-tests.log')}
    require(required <= proof['files'].keys(), 'new CPU proof omits tested collector closure or final log')
    paths = checked_manifest(proof['files']) | {path}
    if 'test_modules' in proof:
        require(set(proof['test_modules']) == {'test_run_stage.py', 'test_interactive.py'} and
                proof['test_modules']['test_run_stage.py'] >= 11 and
                proof['test_modules']['test_interactive.py'] > 0 and
                sum(proof['test_modules'].values()) == proof['tests'],
                'affected CPU method module/count scope differs')
    return proof, paths


def check_capability(path):
    review = read(path)
    paths = checked_manifest(review['files']) | {path}
    spec = importlib.util.spec_from_file_location('frozen_interactive_contract', INTERACTIVE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    contract = module.contract()
    require(review['contract'] == contract, 'reviewed command-specific interactive contract changed')
    require(review.get('scope') and review.get('performance_acceptance') is False,
            'capability review must remain command/lifecycle evidence')
    return contract, paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=BASE / 'diagnostic-v1')
    parser.add_argument('--cpu-proof', type=Path, default=BASE / 'cpu-proof.json')
    parser.add_argument('--capability-review', type=Path, default=BASE / 'interactive-capability.json')
    parser.add_argument('--extra-file', type=Path, action='append', default=[])
    args = parser.parse_args()
    stage, cpu_path, capability_path = args.output.resolve(), args.cpu_proof.resolve(), args.capability_review.resolve()
    require(stage.parent == BASE and not stage.exists(), 'require a new stage directly in this BASE')
    require(DOC.is_file(), 'frozen delivery-stage document missing')
    old, audit, fixture_reuse, parent_paths = check_parent()
    differences = check_dependencies(old)
    cpu_reuse, reused_paths = check_reused_cpu(old)
    cpu, cpu_paths = check_cpu(cpu_path)
    contract, capability_paths = check_capability(capability_path)
    source_commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    require(subprocess.run(['git', 'merge-base', '--is-ancestor', ENTRY_COMMIT, source_commit],
                           cwd=ROOT, check=False).returncode == 0, 'current source does not contain authorized entry')
    # Deliberately no BASE.rglob/glob: mutable preparation stdout is not input.
    paths = set(map(Path, old['files'])) | NEW_TOOLS | parent_paths | reused_paths | cpu_paths | capability_paths | {DOC}
    for path in args.extra_file:
        path = path.resolve()
        require(path.is_file() and not path.is_relative_to(stage), 'explicit extra input missing or inside output')
        paths.add(path)
    stdout_stat = os.fstat(sys.stdout.fileno())
    for path in paths:
        st = path.stat()
        require((st.st_dev, st.st_ino) != (stdout_stat.st_dev, stdout_stat.st_ino),
                'refuse freezing preparation stdout as an input: ' + str(path))
    stage.mkdir(parents=True)
    fixtures = stage / 'fixtures'; fixtures.mkdir()
    plan = copy.deepcopy(old)
    plan.update(version=5, classification='interactive collector lifecycle bug repair and bounded GPU attribution; no runtime optimization acceptance',
        source_commit=source_commit, authorized_entry_commit=ENTRY_COMMIT,
        output=str(stage / 'run'), lease=str(stage / 'lease'), source_snapshot=str(stage / 'source-snapshot'),
        files={}, stage_timeout_seconds=4800, direct_contract_timeout_seconds=600,
        fixture_variants=VARIANTS, fixture_reuse=fixture_reuse, fixture_new_captures=2,
        fixture_reused_local_controls=2, fixture_candidate='wrapper-interactive',
        lifecycle_dependency_changes=differences,
        prior_cpu_proof=old['cpu_proof'], cpu_reuse=cpu_reuse,
        cpu_proof=dict(path=str(cpu_path), sha256=sha(cpu_path), tests=cpu['tests'], suite_runs=cpu['suite_runs']),
        new_stage_cpu_budget=dict(max_test_methods=40, first_suite_seconds=120,
                                 max_suite_runs=2, max_elapsed_seconds=240),
        new_stage_native_budget=dict(max_children=0, max_elapsed_seconds=0),
        prior_aborted_stage=dict(plan=str(PRIOR_STAGE / 'frozen-plan.json'), plan_sha256=PLAN_SHA,
            audit=str(PRIOR / 'audit-rerun.json'), audit_sha256=AUDIT_SHA,
            source_validity='ENV_ABORT', source_state='STOPPED', pooled_new_samples=False,
            scope='Historical local fixture contracts and unchanged normal/CPU evidence only; no restoration of prior environment validity.'),
        first_test='Affected interactive/runner CPU contracts first; unchanged monitor/native and local stop-control proofs reused. Then two new interactive controls before conditional six HTTP.',
        stop='First known-work, negative, identity, lifecycle, monitor or HTTP failure. Only wrapper-interactive with clean actual-target diagnostics, terminal evidence and full memory coverage may authorize model capture. No retry.',
        acceptance_scope='Collector repair and development diagnostics only. Runtime candidate/five-tier optimization acceptance requires a separate freeze; no automatic next stage or delivery.',
        preparation_input_policy='Explicit inherited/CPU/capability/reference paths only; output BASE and stdout are not recursively frozen.')
    plan['nsys']['interactive_contract'] = contract
    plan['prior_monitor_fix'] = copy.deepcopy(old['monitor_fix'])
    plan['monitor_fix'].update(runner_sha256=sha(RUNNER),
        runner_cpu_proof=dict(path=str(cpu_path), sha256=sha(cpu_path)),
        scope='Unchanged monitor policy and monitor/native proofs reused. Changed '
              'runner transitions and final barriers are bound to the new affected '
              'CPU proof; pending still is not a terminal or complete resource sample.')
    plan['steps'][0]['timeout'] = 600
    copied = {}
    for step in plan['steps']:
        for case in step['cases']:
            source = Path(case['path']); target = fixtures / source.name
            if target not in copied:
                shutil.copy2(source, target); copied[target] = sha(source)
            require(sha(target) == copied[target] == sha(source), 'fixture copy identity/collision')
            case['path'] = str(target)
        step['command'] = [sys.executable, '-B', str(RUNNER), '--plan',
                           str(stage / 'frozen-plan.json'), '--custom-step', step['name']]
    require(sum(s['http_requests'] for s in plan['steps']) == plan['http_requests'] == 6 and
            sum(s['http_loads'] for s in plan['steps']) == plan['http_loads'] == 4 and
            plan['retries'] == plan['warmups'] == plan['observation_build_limit'] == plan['runtime_optimization_candidates'] == 0,
            'frozen request/build/retry budget changed')
    paths |= set(copied)
    snapshot = Path(plan['source_snapshot']); snapshot.mkdir()
    for path in sorted(paths):
        digest = sha(path); plan['files'][str(path)] = digest
        if path.is_relative_to(ROOT) and '.q4t-work' not in path.relative_to(ROOT).parts:
            dest = snapshot / path.relative_to(ROOT)
            dest.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(path, dest)
            require(sha(dest) == digest, 'source copy changed during preparation')
    (snapshot / 'worktree.patch').write_bytes(subprocess.check_output(['git', 'diff', 'HEAD'], cwd=ROOT))
    checked_manifest(plan['files'])
    save(stage / 'frozen-plan.json', plan)
    print(json.dumps(dict(plan=str(stage / 'frozen-plan.json'), sha256=sha(stage / 'frozen-plan.json'),
        frozen_files=len(plan['files']), http_requests=6, model_starts=4,
        new_fixture_captures=2, reused_local_controls=2, seconds=4800,
        note='Preparation only; no test, GPU, HTTP, sensor sample, model build or deployment.'), indent=2))


if __name__ == '__main__':
    main()

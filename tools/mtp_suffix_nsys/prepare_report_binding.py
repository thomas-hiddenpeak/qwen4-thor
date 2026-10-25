"""Freeze one synchronous report-binding repair; never execute tests or captures.

Reuse the large historical snapshot and fixed Nsight package by identity. Only
the explicitly permitted collector sources and documentation get a new overlay.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import synchronous
import prepare_synchronous as synchronous_prior

automatic_prior = synchronous_prior.automatic_prior
prior = automatic_prior.prior

ROOT, HERE = prior.ROOT, prior.HERE
SELF = Path(__file__).resolve()
BASE = ROOT / '.q4t-work/suffix-report-binding-20261026'
ENTRY_COMMIT = 'a7b24756eb1ee256ecdb4478094fcc0554691558'
PARENT = synchronous_prior.BASE / 'diagnostic-v1'
PARENT_PLAN_SHA = '089c5616f5e21cf564feabf4f5b80b7904a26f448feaff455c168a299acd1ed6'
PARENT_AUDIT = synchronous_prior.BASE / 'audit-v1/audit-stage.json'
PARENT_AUDIT_SHA = '0730b24d82248a6c97970c22d260d7a4839bc8efc616f3f11ae5eaedfd75d7c1'
DOC = ROOT / 'docs/MTP_SUFFIX_REPORT_BINDING_2026-10-26.md'
RUNNER = HERE / 'run_stage.py'
CHANGED = {HERE / 'synchronous.py', HERE / 'test_synchronous.py'}
NEW = {SELF}
CPU_MODULES = {'test_run_stage.py', 'test_synchronous.py'}
OFFLINE_REVIEW = synchronous_prior.BASE / 'offline-sync-review.json'
OFFLINE_REVIEW_SHA = 'ce88f06780a40eb845ed087e922c23ed5c6698939b4a2f871bc266f5062ad907'
BINDING_DESIGN = synchronous_prior.BASE / 'next-binding-fix-design.json'
BINDING_DESIGN_SHA = '73aed3953f90683f2d218440ceee1450aa0303325c1c2d01b8dcb1c9d0480208'
require, sha, read, save = prior.require, prior.sha, prior.read, prior.save
checked_manifest = prior.checked_manifest


def check_recent_parent():
    plan_path = PARENT / 'frozen-plan.json'
    require(sha(plan_path) == PARENT_PLAN_SHA and sha(PARENT_AUDIT) == PARENT_AUDIT_SHA,
            'fixed synchronous-stop aborted parent identity changed')
    plan, audit = read(plan_path), read(PARENT_AUDIT)
    state_path, seal_path = PARENT / 'run/state.json', PARENT / 'run/files.json'
    state, seal = read(state_path), read(seal_path)
    require(audit['evidence_integrity'] == 'PASS' and not audit['errors'] and
            audit['evidence_validity'] == 'ENV_ABORT' and
            audit['measurement_complete'] is False and
            audit['actual_http_requests'] == audit['actual_model_loads'] == 0 and
            audit['identities']['plan_sha256'] == state['plan_sha256'] == PARENT_PLAN_SHA and
            audit['identities']['state_sha256'] == sha(state_path) and
            audit['identities']['raw_seal_sha256'] == sha(seal_path) and
            state['status'] == 'STOPPED' and not state['completed'] and
            state['requests_completed'] == state['loads_started'] == 0,
            'parent validity or zero-model terminal evidence changed')
    actual = {str(p.relative_to(PARENT / 'run')) for p in (PARENT / 'run').rglob('*')
              if p.is_file() and p.name != 'files.json'}
    require(actual == set(seal), 'synchronous-stop parent raw inventory changed')
    paths = checked_manifest(seal, PARENT / 'run')
    paths |= checked_manifest(audit['identities']['terminal_external_files'])
    for field in ('source_snapshot_reuse', 'prepare_overlay'):
        ref = plan[field]
        require(sha(ref['path']) == ref['sha256'], 'parent snapshot mapping changed')
        paths.add(Path(ref['path']))
    overlay = read(plan['prepare_overlay']['path'])
    for source, row in overlay['files'].items():
        require(sha(row['copy']) == row['sha256'] == plan['files'][source],
                'parent overlay copy changed')
        paths.add(Path(row['copy']))
    patch = PARENT / 'prepare-overlay/worktree.patch'
    require(sha(patch) == overlay['worktree_patch_sha256'], 'parent overlay patch changed')
    paths |= {plan_path, PARENT_AUDIT, PARENT_AUDIT.with_suffix('.md'),
              state_path, seal_path, patch}
    prior.check_scope(plan)
    return plan, paths


def documentation(path):
    return ((path.is_relative_to(ROOT / 'docs') and path.suffix in ('.md', '.json')) or
            path == HERE / 'README.md')


def check_live(plan):
    differences = []
    for name, before in plan['files'].items():
        path, after = Path(name), sha(name)
        if after != before:
            require(path in CHANGED or documentation(path),
                    'non-whitelisted dependency changed: ' + name)
            differences.append(dict(path=name, before=before, after=after,
                role='collector_execution' if path in CHANGED else 'documentation'))
    inherited = {Path(p) for p in plan['files'] if Path(p).parent == HERE and
                 Path(p).suffix == '.py'}
    require({p.resolve() for p in HERE.glob('*.py')} == inherited | NEW,
            'unexpected collector Python inventory')
    require(all(p.is_file() and not p.is_symlink() for p in NEW),
            'new collector sources must be regular local files')
    expected = {Path(p) for p in plan['files'] if Path(p) == ROOT / 'CMakeLists.txt' or
                any(Path(p).is_relative_to(ROOT / f) for f in ('src', 'include', 'cmake'))}
    actual = {ROOT / 'CMakeLists.txt'}
    for folder in ('src', 'include', 'cmake'):
        actual |= {p for p in (ROOT / folder).rglob('*') if p.is_file()}
    require(expected == actual, 'inference runtime inventory changed')
    for field, digest in [('candidate', 'candidate_sha256'),
                          ('reference_binary', 'reference_binary_sha256'),
                          ('fixture_binary', 'fixture_binary_sha256')]:
        require(sha(plan[field]) == plan[digest], field + ' identity changed')
    require(plan['candidate_source_commit'] == '55363f18dedb947ac52e69e10475d3082cb71a82' and
            plan['candidate_sha256'] == 'f0ff0fe8e11791d561bc49f4dc42762415d4ff000836bbd8c38548723906c914' and
            sha(Path(plan['artifact']) / 'manifest.json') == plan['subset_manifest_sha256'],
            'observation runtime or subset head changed')
    for name, expected in plan['model_file_metadata'].items():
        stat = Path(name).stat()
        require(dict(size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                     device=stat.st_dev, inode=stat.st_ino) == expected,
                'read-only model metadata changed: ' + name)
    return differences


def check_cpu(plan, path):
    proof = read(path)
    modules = proof['test_modules']
    require(set(modules) == CPU_MODULES and modules['test_run_stage.py'] == 19 and
            0 < modules['test_synchronous.py'] <= 16 and
            sum(modules.values()) == proof['tests'] <= 35 and
            proof['failed'] == proof['errors'] == proof['skipped'] == 0 and
            proof['suite_runs'] in (1, 2) and 0 < proof['elapsed_seconds'] <= 180,
            'affected CPU proof outcome, modules or budget differs')
    paths = checked_manifest(proof['files']) | {path}
    require(str(SELF) in proof['files'], 'CPU freeze must include this preparer')
    for name in ('interactive.py', 'test_interactive.py', 'run_stage.py',
                 'test_run_stage.py', 'automatic.py', 'report_ready.py', 'test_automatic.py',
                 'synchronous.py', 'test_synchronous.py'):
        require(str(HERE / name) in proof['files'], 'CPU dependency missing: ' + name)
    if proof['suite_runs'] == 2:
        ref = proof['targeted_failure_receipt']
        require(sha(ref['path']) == ref['sha256'], 'targeted repair receipt changed')
        failure = read(ref['path'])
        require(failure.get('failure') or failure.get('returncode') not in (None, 0) or
                failure.get('sources_changed') or failure.get('cleanup_errors') or
                failure.get('source_read_errors') or failure.get('temporary_remaining') or
                failure.get('failed', 0) + failure.get('errors', 0) > 0,
                'second suite requires recorded concrete failure')
        paths.add(Path(ref['path']))
    previous_ref = plan['cpu_proof']
    require(sha(previous_ref['path']) == previous_ref['sha256'], 'historical CPU62 proof changed')
    previous = read(previous_ref['path'])
    require(previous['tests'] == 62 and previous['failed'] == previous['errors'] ==
            previous['skipped'] == 0 and previous['test_modules'] == {
                'test_interactive.py': 16, 'test_automatic.py': 14,
                'test_run_stage.py': 19, 'test_synchronous.py': 13},
            'historical CPU62 outcome changed')
    for name in ('interactive.py', 'test_interactive.py', 'automatic.py',
                 'report_ready.py', 'test_automatic.py'):
        require(sha(HERE / name) == previous['files'][str(HERE / name)],
                'reused manual/automatic CPU dependency changed: ' + name)
    old_ref = plan['cpu_reuse']['cpu97_proof']
    require(sha(old_ref['path']) == old_ref['sha256'], 'reused CPU97 proof changed')
    old = read(old_ref['path'])
    require(old['tests'] == 97 and old['failed'] == old['errors'] == old['skipped'] == 0,
            'reused CPU97 outcome changed')
    for name in plan['cpu_reuse']['reused_methods']:
        require(sha(name) == old['files'][name], 'unchanged reused CPU module changed')
    native_ref = plan['native_monitor_proof']
    require(sha(native_ref['path']) == native_ref['sha256'], 'native64 proof changed')
    native = read(native_ref['path'])
    require(native['passed'] is True and native['completed_children'] == native['children_started'] == 64 and
            native['cleanup_complete'] is True and native['all_owned_reaped'] is True and
            not native['failures'] and
            native['monitor_sha256'] == sha(ROOT / 'tools/mtp_suffix_zero_hit/telemetry.py') and
            native['tool_sha256'] == sha(HERE / 'reproduce_exit_pending.py') and
            sha(native['rows']) == native['rows_sha256'], 'unchanged native64 identity/cleanup differs')
    kernel_ref = plan['monitor_fix']['kernel_contract']
    require(sha(kernel_ref['path']) == kernel_ref['sha256'], 'kernel contract changed')
    kernel = read(kernel_ref['path'])
    require(kernel['release'] == os.uname().release and kernel['PF_EXITING'] == 4 and
            sha(kernel['original_path']) == kernel['sha256'] == sha(kernel['copy_path']),
            'live exit-pending kernel contract changed')
    paths |= {Path(previous_ref['path']), Path(old_ref['path']), Path(native_ref['path']),
              Path(native['rows']), Path(kernel_ref['path']), Path(kernel['original_path']),
              Path(kernel['copy_path'])}
    return proof, paths


def check_review(path, parent):
    review = read(path)
    require(review['contract'] == synchronous.contract() and
            review['contract']['mode'] == 'interactive_api_sync_stop' and
            review['contract']['schema'] == 4 and
            review['contract']['report_binding_revision'] == 1 and
            review['nsys_path'] == str(prior.CLI) and review['nsys_sha256'] == prior.CLI_SHA and
            review['version'] == 'NVIDIA Nsight Systems version ' + prior.VERSION and
            review['options_supported'] is True and review['lifecycle_qualified'] is False and
            review['evidence_level'] == 'STATIC_ONLY_NO_CAPTURE' and
            review['trace_backend']['requested'] == 'cuda' and
            review['trace_backend']['actual'] == 'UNOBSERVED' and
            review['trace_backend']['fallback_possible'] is True and
            review['performance_acceptance'] is False and review['scope'],
            'synchronous capability contract differs or claims qualification')
    scope_path = BASE / 'scope.json'
    scope = read(scope_path)
    require(scope['source_commit'] == ENTRY_COMMIT and
            scope['cpu']['max_methods'] == 35 and scope['cpu']['timeout_seconds'] == 180 and
            scope['cpu']['suite_runs'] == scope['cpu']['targeted_repair_runs'] == 1 and
            scope['cpu']['targeted_repair_requires_concrete_failure'] is True and
            scope['gpu']['attempts'] == 1 and scope['gpu']['full_stage_shared_lease'] is True and
            (scope['gpu']['wall_seconds'], scope['gpu']['controls'],
             scope['gpu']['control_group_seconds'], scope['gpu']['fixture_each_seconds'],
             scope['gpu']['model_loads_max'], scope['gpu']['http_requests_max']) ==
            (4800, 2, 600, 300, 4, 6) and
            scope['gpu']['order'] == ['direct-interactive', 'wrapper-interactive',
                'nsys8-off', 'nsys8-on', 'nsys200-on', 'nsys200-off'] and
            scope['builds'] == scope['retries'] == scope['runtime_changes'] == 0 and
            scope['tool_versions'] == 1 and scope['new_cli_options'] == 0 and
            scope['completion_does_not_prove_trace_integrity'] is True,
            'report-binding stage scope/budget differs')
    require(str(scope_path) in review['files'], 'capability must bind stage scope')
    paths = checked_manifest(review['files']) | {path, scope_path}
    # Older research remains in parent files; bind only this defect's new evidence.
    require(sha(OFFLINE_REVIEW) == OFFLINE_REVIEW_SHA and
            sha(BINDING_DESIGN) == BINDING_DESIGN_SHA,
            'fixed offline defect review or proposed binding design changed')
    require({str(OFFLINE_REVIEW), str(BINDING_DESIGN)} <= set(review['files']),
            'capability must explicitly bind offline review and binding design')
    forensic, design = read(OFFLINE_REVIEW), read(BINDING_DESIGN)
    require(forensic['evidence_validity'] == 'FORENSIC_ONLY_ENV_ABORT' and
            forensic['measurement_complete'] is False and
            forensic['performance_acceptance'] is False and
            forensic['candidate_qualified'] is False and
            forensic['stage_status_preserved'] == 'ENV_ABORT' and
            forensic['scope']['gpu_cli_export_test_build_http_invocations'] == 0 and
            forensic['scope']['frozen_files_modified'] is False and
            design['basis'] == dict(path=str(OFFLINE_REVIEW), sha256=OFFLINE_REVIEW_SHA) and
            design['status'] == 'DESIGNED_NOT_IMPLEMENTED_NOT_TESTED' and
            design['new_cli_options'] == design['new_tool_versions'] == 0 and
            design['additional_gpu_attempts_this_stage'] ==
            design['additional_cpu_suites_this_stage'] == 0 and
            design['implemented'] is False and
            design['delivery_authorized_by_this_evidence'] is False,
            'historical forensic evidence has been promoted or changed')
    paths |= checked_manifest({name: row['sha256'] for name, row in
                               forensic['source_files'].items()})
    paths |= {OFFLINE_REVIEW, BINDING_DESIGN}
    # This repair changes filename binding, not CLI choices or control/trace gates.
    invariants = ('mode', 'start_options', 'launch_options', 'stop_options',
        'shutdown_options', 'start_timeout_seconds', 'stop_timeout_seconds',
        'shutdown_timeout_seconds', 'control_cleanup_seconds',
        'control_kill_wait_seconds', 'poll_seconds', 'retries',
        'adopted_reap_timeout_seconds', 'owner', 'ancestry_limit', 'session_name',
        'target_exit_is_independent', 'shutdown_is_not_collection_success',
        'actual_target_warnings_block', 'stop_allowed', 'callback', 'capture_ranges',
        'report_wait_timeout_seconds', 'report_wait_poll_seconds',
        'launch_log_max_bytes', 'binding_receipt_max_bytes', 'completion',
        'target_receipt_sources', 'binding_receipt', 'report_ready_is_trace_completeness',
        'success_order', 'failure_shutdown_cancels', 'manual_stop_fallback')
    inherited = parent['nsys']['interactive_contract']
    require(all(review['contract'][key] == inherited[key] for key in invariants),
            'report-binding repair changed CLI, timing, ownership or trace gates')
    return review, scope, paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=BASE / 'diagnostic-v1')
    parser.add_argument('--cpu-proof', type=Path, default=BASE / 'cpu-proof.json')
    parser.add_argument('--capability-review', type=Path, default=BASE / 'report-binding-capability.json')
    parser.add_argument('--extra-file', type=Path, action='append', default=[])
    args = parser.parse_args()
    stage, cpu_path, capability = args.output.resolve(), args.cpu_proof.resolve(), args.capability_review.resolve()
    require(stage == BASE / 'diagnostic-v1' and not stage.exists() and DOC.is_file() and
            cpu_path == BASE / 'cpu-proof.json' and capability == BASE / 'report-binding-capability.json',
            'fixed new stage/proof/review/document paths required')
    historical, historical_audit, snapshot_files, historical_paths = prior.check_parent()
    _, intermediate_paths = automatic_prior.check_recent_parent()
    _, automatic_paths = synchronous_prior.check_recent_parent()
    old, recent_paths = check_recent_parent()
    differences = check_live(old)
    _, tool, tool_paths = prior.check_tool(prior.BASE / 'interactive-capability.json', historical)
    cpu, cpu_paths = check_cpu(old, cpu_path)
    review, scope, review_paths = check_review(capability, old)
    source_commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    require(subprocess.run(['git', 'merge-base', '--is-ancestor', scope['source_commit'], source_commit],
                           cwd=ROOT, check=False).returncode == 0,
            'source does not contain authorized report-binding entry')
    paths = set(map(Path, old['files'])) | NEW | CHANGED | historical_paths | intermediate_paths | automatic_paths | recent_paths | tool_paths | cpu_paths | review_paths | {DOC}
    for item in args.extra_file:
        path = item.resolve()
        require(path.is_file() and not path.is_relative_to(stage), 'unsafe explicit extra input')
        paths.add(path)
    stdout = os.fstat(sys.stdout.fileno())
    for path in paths:
        require(not path.is_relative_to(stage), 'new output cannot be a preparation input')
        stat = path.stat()
        require((stat.st_dev, stat.st_ino) != (stdout.st_dev, stdout.st_ino),
                'refuse freezing active preparation stdout')
    stage.mkdir(parents=True)
    overlay = stage / 'prepare-overlay'; overlay.mkdir()
    mapping_path = stage / 'source-snapshot-reuse.json'
    save(mapping_path, dict(parent_plan=str(PARENT / 'frozen-plan.json'),
        parent_plan_sha256=PARENT_PLAN_SHA, audit_path=str(PARENT_AUDIT), audit_sha256=PARENT_AUDIT_SHA,
        source_snapshot=historical['source_snapshot'], files=snapshot_files,
        snapshot_parent_plan=str(prior.PRIOR_STAGE / 'frozen-plan.json'),
        snapshot_parent_plan_sha256=prior.PLAN_SHA,
        worktree_patch_sha256=historical_audit['identities']['source_snapshot']['worktree_patch_sha256'],
        inherited_snapshot_mapping=old['source_snapshot_reuse'],
        inherited_overlay=old['prepare_overlay'],
        scope='Historical source snapshot/overlay referenced by identity; no large venv/source recopy.'))
    overlay_sources = NEW | CHANGED | {DOC} | {Path(row['path']) for row in differences}
    copies = {}
    for path in sorted(overlay_sources):
        target = overlay / path.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = sha(path); shutil.copy2(path, target)
        require(sha(target) == digest == sha(path), 'overlay source changed during copy')
        role = ('preparation_only' if path == SELF else
                'documentation' if documentation(path) else 'collector_execution_or_cpu_contract')
        copies[str(path)] = dict(copy=str(target), sha256=digest, role=role)
        paths.add(target)
    patch = overlay / 'worktree.patch'
    patch.write_bytes(subprocess.check_output(['git', 'diff', 'HEAD'], cwd=ROOT))
    overlay_path = stage / 'prepare-overlay.json'
    save(overlay_path, dict(files=copies, worktree_patch_sha256=sha(patch),
        scope='Explicit changed/new collector and documentation copies include untracked additions; inference runtime unchanged.'))
    paths |= {mapping_path, overlay_path, patch}
    plan = copy.deepcopy(old)
    plan.pop('cpu32_reuse', None)
    plan.update(version=9, classification='Fixed synchronous report-binding defect repair; conditional GPU diagnostics only',
        source_commit=source_commit, authorized_entry_commit=scope['source_commit'],
        output=str(stage / 'run'), lease=str(stage / 'lease'), files={},
        source_snapshot_reuse=dict(path=str(mapping_path), sha256=sha(mapping_path)),
        prepare_overlay=dict(path=str(overlay_path), sha256=sha(overlay_path)),
        preparation_dependency_changes=differences, lifecycle_dependency_changes=differences,
        prior_nsys=copy.deepcopy(old['nsys']), prior_cpu_proof=old['cpu_proof'],
        cpu_proof=dict(path=str(cpu_path), sha256=sha(cpu_path), tests=cpu['tests'], suite_runs=cpu['suite_runs']),
        historical_cpu62=dict(**old['cpu_proof'], scope='Prior synchronous binding only; changed binding requires current CPU proof. Unchanged manual16/automatic14 contracts reused by dependency identity.'),
        unchanged_manual_automatic_cpu_reuse=dict(proof=old['cpu_proof'],
            methods={'test_interactive.py': 16, 'test_automatic.py': 14},
            method_count=30, scope='CPU contracts only; no inherited GPU lifecycle qualification.'),
        new_stage_cpu_budget=dict(max_test_methods=35, first_suite_seconds=180,
            max_suite_runs=2, max_elapsed_seconds=180, second_suite_requires_concrete_failure=True),
        new_stage_native_budget=dict(max_children=0, max_elapsed_seconds=0),
        stage_scope=dict(path=str(BASE / 'scope.json'), sha256=sha(BASE / 'scope.json')),
        prior_aborted_stage=dict(plan=str(PARENT / 'frozen-plan.json'), plan_sha256=PARENT_PLAN_SHA,
            audit=str(PARENT_AUDIT), audit_sha256=PARENT_AUDIT_SHA, source_validity='ENV_ABORT',
            source_state='STOPPED', pooled_new_samples=False),
        first_test='Affected report-binding and unchanged runner integration CPU contracts, then two fixed synchronous-stop fixtures, then conditional six HTTP.',
        acceptance_scope='Collector lifecycle repair and development GPU diagnostics only; no suffix performance acceptance or deployment.',
        preparation_input_policy='Explicit parent/CPU/package/research/capability inputs; immutable snapshot plus small execution overlay. No BASE recursion.')
    tool.update(capability_path=str(capability), capability_sha256=sha(capability),
                scope='Same isolated 2026.5.1 package and CLI options; revised report binding remains unqualified until controls pass.')
    plan['tool_dependency'] = tool
    plan['nsys'].update(path=str(prior.CLI), sha256=prior.CLI_SHA, capability_path=str(capability),
        capability_sha256=sha(capability), capabilities=copy.deepcopy(review),
        trace_backend=copy.deepcopy(review['trace_backend']), interactive_contract=review['contract'])
    plan['prior_monitor_fix'] = copy.deepcopy(old['monitor_fix'])
    plan['monitor_fix'].update(runner_sha256=sha(RUNNER),
        runner_cpu_proof=dict(path=str(cpu_path), sha256=sha(cpu_path)),
        scope='Unchanged monitor/native and runner policy; current CPU proof covers report binding and runner integration. Pending is never a terminal/resource-complete sample.')
    for step in plan['steps']:
        for case in step['cases']:
            require(sha(case['path']) == old['files'][case['path']], 'reused HTTP case changed')
        step['command'] = [sys.executable, '-B', str(RUNNER), '--plan',
                           str(stage / 'frozen-plan.json'), '--custom-step', step['name']]
    prior.check_scope(plan)
    for path in sorted(paths):
        plan['files'][str(path)] = sha(path)
    checked_manifest(plan['files'])
    save(stage / 'frozen-plan.json', plan)
    print(json.dumps(dict(plan=str(stage / 'frozen-plan.json'), sha256=sha(stage / 'frozen-plan.json'),
        frozen_files=len(plan['files']), package_files=1765, current_cpu_methods=cpu['tests'],
        reused_cpu_methods=86, reused_manual_automatic_methods=30, source_snapshot_reused=plan['source_snapshot'], overlay_files=len(copies),
        http_requests=6, model_starts=4, new_fixture_captures=2, seconds=4800,
        note='Preparation only; no test, profiler, export, GPU, HTTP, build, installation or deployment.'), indent=2))


if __name__ == '__main__':
    main()

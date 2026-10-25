"""Freeze one isolated Nsight 2026.5.1 dependency qualification; never run it.

The execution sources, CPU32 proof, and large source snapshot are reused by
identity. Only this preparer and documentation receive a small new overlay.
No profiler, test, model, package installer, or export command is invoked here.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
SELF = Path(__file__).resolve()
BASE = ROOT / '.q4t-work/suffix-nsys-265-20261026'
DEPENDENCY = ROOT / '.q4t-work/suffix-nsys-dependency-20261026'
PACKAGE_ROOT = DEPENDENCY / 'extracted'
PRIOR = ROOT / '.q4t-work/suffix-delivery-20261026'
PRIOR_STAGE = PRIOR / 'diagnostic-v1'
PLAN_SHA = '4ea7b25871c94993ad03af9b6d3981039eebd70efbc4a3b45f2e128116f328c1'
AUDIT_SHA = '6fba3a331b103612a1607ccd5ef8da9e434e971e6c7a60b9ab41c0d1285b25b2'
VERSION = '2026.5.1.161-265138896106v0'
PACKAGE_SHA = '087c08b5ddf3ed6f913748f8206d6bbdc225f2aa275ff33af4c487e8b319c8ab'
PACKAGE_MANIFEST_SHA = '876d4201d6d7d5dba20eeaa77d988c722fbc0862dfc4d6dc015ed47352d7a1d1'
CLI = PACKAGE_ROOT / 'opt/nvidia/nsight-systems-cli/2026.5.1/target-linux-sbsa-armv8/nsys'
CLI_SHA = 'dbe02661b00efbfb4c9c3cd3c2d8df6fecd6e80ac794305892e233e0d712a527'
RUNNER = HERE / 'run_stage.py'
DOC = ROOT / 'docs/MTP_SUFFIX_NSYS_265_2026-10-26.md'
VARIANTS = [dict(name='direct-interactive', wrapper=False, external=True,
                 lifecycle='interactive'),
            dict(name='wrapper-interactive', wrapper=True, external=True,
                 lifecycle='interactive')]
MODEL_STEPS = [('nsys8-off', False, 8170, 1), ('nsys8-on', True, 8170, 1),
               ('nsys200-on', True, 204781, 2), ('nsys200-off', False, 204781, 2)]


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
    for name, digest in values.items():
        path = Path(name)
        if base is None:
            require(path.is_absolute(), 'absolute frozen path required')
        else:
            require(not path.is_absolute() and '..' not in path.parts,
                    'unsafe sealed relative path')
            path = base / path
            require(not path.is_symlink(), 'sealed evidence cannot be a symlink')
        require(path.is_file() and sha(path) == digest, 'input changed: ' + str(path))
        paths.add(path)
    return paths


def check_parent():
    plan_path = PRIOR_STAGE / 'frozen-plan.json'
    audit_path = PRIOR / 'audit-delivery.json'
    require(sha(plan_path) == PLAN_SHA and sha(audit_path) == AUDIT_SHA,
            'parent plan or audit identity changed')
    plan, audit = read(plan_path), read(audit_path)
    state_path, seal_path = PRIOR_STAGE / 'run/state.json', PRIOR_STAGE / 'run/files.json'
    state, seal = read(state_path), read(seal_path)
    post = read(PRIOR_STAGE / 'postflight.json')
    outer = read(PRIOR_STAGE / 'lease/exit.json')
    events = [json.loads(line) for line in
              (PRIOR_STAGE / 'lease/events.jsonl').read_text().splitlines()]
    require(audit['evidence_integrity'] == 'PASS' and not audit['errors'] and
            audit['evidence_validity'] == post['status'] == 'ENV_ABORT' and
            audit['measurement_complete'] is False and
            audit['identities']['plan_sha256'] == state['plan_sha256'] == PLAN_SHA and
            audit['identities']['state_sha256'] == post['state_sha256'] == sha(state_path) and
            audit['identities']['raw_seal_sha256'] == sha(seal_path) and
            state['status'] == 'STOPPED' and not state['completed'] and
            state['requests_completed'] == state['loads_started'] == 0 and
            outer['status'] == 'COMMAND_FAILED' and outer['child_returncode'] == 1 and
            events[-1]['event'] == 'lease_released',
            'parent ENV_ABORT/zero-model terminal evidence disagrees')
    run = PRIOR_STAGE / 'run'
    present = {str(p.relative_to(run)) for p in run.rglob('*')
               if p.is_file() and p.name != 'files.json'}
    require(set(seal) == present, 'parent raw inventory changed')
    paths = checked_manifest(seal, run)
    snapshot = Path(plan['source_snapshot'])
    expected = {str(Path(p).relative_to(ROOT)): digest for p, digest in plan['files'].items()
                if Path(p).is_relative_to(ROOT) and
                '.q4t-work' not in Path(p).relative_to(ROOT).parts}
    actual = {str(p.relative_to(snapshot)) for p in snapshot.rglob('*') if p.is_file()}
    require(actual == set(expected) | {'worktree.patch'}, 'parent snapshot inventory changed')
    paths |= checked_manifest(expected, snapshot)
    patch_sha = audit['identities']['source_snapshot']['worktree_patch_sha256']
    require(sha(snapshot / 'worktree.patch') == patch_sha, 'parent snapshot patch changed')
    paths |= {plan_path, audit_path, PRIOR / 'audit-delivery.md', seal_path, state_path,
              PRIOR_STAGE / 'postflight.json', PRIOR_STAGE / 'lease/exit.json',
              PRIOR_STAGE / 'lease/events.jsonl', snapshot / 'worktree.patch'}
    return plan, audit, expected, paths


def check_live_dependencies(old):
    changes = []
    for name, before in old['files'].items():
        path, after = Path(name), sha(name)
        if before == after:
            continue
        documentation = ((path.is_relative_to(ROOT / 'docs') and path.suffix in ('.md', '.json'))
                         or path == HERE / 'README.md')
        require(documentation, 'execution dependency changed: ' + name)
        changes.append(dict(path=name, before=before, after=after,
                            role='documentation_only_not_reused_as_old_evidence'))
    inherited = {Path(p) for p in old['files'] if Path(p).parent == HERE and Path(p).suffix == '.py'}
    require({p.resolve() for p in HERE.glob('*.py')} == inherited | {SELF},
            'unexpected collector Python module outside preparation-only addition')
    runtime = {ROOT / 'CMakeLists.txt'}
    for folder in ('src', 'include', 'cmake'):
        runtime |= {p for p in (ROOT / folder).rglob('*') if p.is_file()}
    expected = {Path(p) for p in old['files'] if Path(p) == ROOT / 'CMakeLists.txt' or
                any(Path(p).is_relative_to(ROOT / f) for f in ('src', 'include', 'cmake'))}
    require(runtime == expected, 'runtime source/header inventory changed')
    for field, digest_field in [('candidate', 'candidate_sha256'),
                                ('reference_binary', 'reference_binary_sha256'),
                                ('fixture_binary', 'fixture_binary_sha256')]:
        require(sha(old[field]) == old[digest_field], field + ' changed')
    require(old['candidate_source_commit'] == '55363f18dedb947ac52e69e10475d3082cb71a82' and
            old['candidate_sha256'] == 'f0ff0fe8e11791d561bc49f4dc42762415d4ff000836bbd8c38548723906c914',
            'observation runtime identity changed')
    require(sha(Path(old['artifact']) / 'manifest.json') == old['subset_manifest_sha256'],
            'subset head identity changed')
    for name, expected in old['model_file_metadata'].items():
        stat = Path(name).stat()
        require(dict(size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                     device=stat.st_dev, inode=stat.st_ino) == expected,
                'model metadata changed: ' + name)
    return changes


def check_cpu_reuse(old):
    path = Path(old['cpu_proof']['path'])
    proof = read(path)
    require(sha(path) == old['cpu_proof']['sha256'] and proof['tests'] == 32 and
            proof['test_modules'] == {'test_run_stage.py': 16, 'test_interactive.py': 16} and
            proof['suite_runs'] == 1 and 0 < proof['elapsed_seconds'] <= 120 and
            proof['failed'] == proof['errors'] == proof['skipped'] == 0,
            'unchanged CPU32 proof required')
    require(SELF not in {Path(p) for p in proof['files']},
            'new preparation-only file cannot be represented as previously tested')
    paths = checked_manifest(proof['files']) | {path}
    for name in ('run_stage.py', 'interactive.py', 'test_run_stage.py', 'test_interactive.py'):
        require(str(HERE / name) in proof['files'], 'CPU32 dependency missing: ' + name)
    # The parent explicitly separates CPU32 from unchanged monitor/fixture/analyzer
    # and native evidence. Preserve those references and recheck their full ledgers.
    cpu97 = old['cpu_reuse']['cpu97_proof']
    require(sha(cpu97['path']) == cpu97['sha256'], 'prior CPU97 proof changed')
    prior = read(cpu97['path'])
    require(prior['tests'] == 97 and prior['failed'] == prior['errors'] == prior['skipped'] == 0,
            'prior CPU97 outcome changed')
    for name in old['cpu_reuse']['reused_methods']:
        require(sha(name) == prior['files'][name], 'unchanged reused module changed: ' + name)
    native_ref = old['native_monitor_proof']
    require(sha(native_ref['path']) == native_ref['sha256'], 'native64 proof changed')
    native = read(native_ref['path'])
    require(native['passed'] is True and native['completed_children'] == native['children_started'] == 64 and
            native['cleanup_complete'] is True and native['all_owned_reaped'] is True and not native['failures'] and
            native['monitor_sha256'] == sha(ROOT / 'tools/mtp_suffix_zero_hit/telemetry.py') and
            native['tool_sha256'] == sha(HERE / 'reproduce_exit_pending.py') and
            sha(native['rows']) == native['rows_sha256'], 'native64 identity/cleanup changed')
    paths |= {Path(cpu97['path']), Path(native_ref['path']), Path(native['rows'])}
    return proof, paths


def check_tool(capability_path, old):
    manifest_path = DEPENDENCY / 'extracted-manifest.json'
    require(sha(manifest_path) == PACKAGE_MANIFEST_SHA, 'extracted package manifest changed')
    manifest = read(manifest_path)
    require(len(manifest) == 1765, 'fixed package file count changed')
    present = {str(p.relative_to(PACKAGE_ROOT)) for p in PACKAGE_ROOT.rglob('*')
               if p.is_file() or p.is_symlink()}
    require(present == set(manifest), 'extracted package inventory changed')
    paths, links = set(), {}
    for name, item in manifest.items():
        relative = Path(name)
        require(not relative.is_absolute() and '..' not in relative.parts, 'unsafe package path')
        path = PACKAGE_ROOT / relative
        require(path.resolve().is_relative_to(PACKAGE_ROOT) and path.is_file() and
                path.is_symlink() == item['symlink'] and path.stat().st_size == item['bytes'] and
                sha(path) == item['sha256'], 'package file changed: ' + name)
        paths.add(path)
        if path.is_symlink():
            links[name] = dict(link=os.readlink(path), resolved=str(path.resolve()),
                               sha256=item['sha256'])
    result_path = DEPENDENCY / 'result-v2.json'
    result = read(result_path)
    package = result['package']
    require(result['status'] == 'PREPARED_NOT_QUALIFIED' and
            package['Package'] == 'nsight-systems-cli-2026.5.1' and
            package['Version'] == VERSION and package['Architecture'] == 'arm64' and
            package['SHA256'] == PACKAGE_SHA and result['extracted_files'] == 1765 and
            result['manifest_sha256'] == PACKAGE_MANIFEST_SHA and
            result['profiler_invocations'] == result['gpu_stages'] == result['http_requests'] == 0 and
            result['system_mutations'] is False, 'package preparation receipt differs')
    deb = DEPENDENCY / package['Filename']
    require(deb.parent == DEPENDENCY and sha(deb) == PACKAGE_SHA and
            deb.stat().st_size == int(package['Size']), 'fixed downloaded package changed')
    for request in result['requests']:
        path = Path(request['output'])
        require(path.parent == DEPENDENCY and sha(path) == request['sha256'] and
                path.stat().st_size == request['bytes'], 'official metadata/package receipt changed')
    require(sha(DEPENDENCY / 'scope.json') == result['scope_sha256'], 'package scope changed')
    # Nonrecursive, already completed package-preparation receipts only. Never
    # enumerate the new stage or the mutable preparation stdout directory.
    paths |= {p for p in DEPENDENCY.iterdir() if p.is_file()}
    scope_path, collected_path = BASE / 'capability-scope.json', BASE / 'capability-result.json'
    scope, collected = read(scope_path), read(collected_path)
    commands = [[str(CLI), '--version']] + [[str(CLI), name, '--help'] for name in
                ('start', 'launch', 'stop', 'shutdown', 'profile', 'export')]
    require(scope['version'] == VERSION and scope['commands'] == commands and
            scope['max_commands'] == 7 and scope['retries'] == 0 and
            scope['manifest_sha256'] == PACKAGE_MANIFEST_SHA and
            Path(scope['package_root']) == PACKAGE_ROOT and Path(scope['manifest']) == manifest_path and
            collected['scope_sha256'] == sha(scope_path) and
            collected['status'] == 'STATIC_CAPABILITY_COLLECTED_NOT_QUALIFIED' and
            collected['package_files_verified'] == 1765 and collected['package_unchanged'] is True and
            [r['argv'] for r in collected['commands']] == commands and
            0 < collected['elapsed_seconds'] <= 180,
            'fixed static capability evidence differs')
    for row in collected['commands']:
        path = Path(row['output'])
        require(path.parent == BASE and row['returncode'] == 0 and
                0 < row['elapsed_seconds'] <= 20 and sha(path) == row['sha256'],
                'static CLI command output changed')
        paths.add(path)
    require(sha(BASE / 'check_capability.py') == scope['driver_sha256'], 'capability driver changed')
    require(not CLI.is_symlink() and sha(CLI) == CLI_SHA, 'exact target ELF changed')
    with CLI.open('rb') as stream:
        require(stream.read(4) == b'\x7fELF', 'collector entry must be the target ELF')
    require(VERSION in (BASE / 'version.txt').read_text(), 'reported version differs')
    review = read(capability_path)
    require(review['contract'] == old['nsys']['interactive_contract'] and
            review['nsys_path'] == str(CLI) and review['nsys_sha256'] == CLI_SHA and
            review['version'] == 'NVIDIA Nsight Systems version ' + VERSION and
            review['evidence_level'] == 'STATIC_ONLY_NO_CAPTURE' and
            review['options_supported'] is True and review['lifecycle_qualified'] is False and
            review['trace_backend']['requested'] == 'cuda' and
            review['trace_backend']['actual'] == 'UNOBSERVED' and
            review['trace_backend']['fallback_possible'] is True and
            review['scope'] and review['performance_acceptance'] is False,
            'new tool command contract must preserve the tested execution lifecycle')
    paths |= checked_manifest(review['files']) | {capability_path, scope_path, collected_path,
                                                  BASE / 'check_capability.py'}
    require({str(p) for p in (scope_path, collected_path)} <= set(review['files']),
            'capability review must bind this version-specific static evidence')
    identity = dict(version=VERSION, path=str(CLI), sha256=CLI_SHA,
        package_path=str(deb), package_sha256=PACKAGE_SHA, package_root=str(PACKAGE_ROOT),
        manifest=str(manifest_path), manifest_sha256=PACKAGE_MANIFEST_SHA,
        package_files=1765, symlinks=links, result_path=str(result_path),
        result_sha256=sha(result_path), capability_path=str(capability_path),
        capability_sha256=sha(capability_path), qualified=False,
        scope='One isolated tool dependency; static CLI success is not collector qualification.')
    return review, identity, paths


def check_scope(plan):
    require((plan['http_requests'], plan['http_loads'], plan['stage_timeout_seconds'],
             plan['direct_contract_timeout_seconds']) == (6, 4, 4800, 600) and
            plan['fixture_variants'] == VARIANTS and plan['fixture_new_captures'] == 2 and
            plan['fixture_reused_local_controls'] == 2 and plan['fixture_candidate'] == 'wrapper-interactive' and
            set(plan['fixture_reuse']) == {'direct-stop', 'wrapper-stop'} and
            plan['fixture_timeout_seconds'] == 300 and
            all(plan[k] == 0 for k in ('retries', 'warmups', 'observation_build_limit',
                                     'runtime_optimization_candidates')),
            'fixed request/lifecycle/budget scope changed')
    require(len(plan['steps']) == 5 and plan['steps'][0]['name'] == 'fixture-controls' and
            plan['steps'][0]['timeout'] == 600 and plan['steps'][0]['http_requests'] == 0,
            'fixture controls must precede model groups')
    for step, (name, suffix, tokens, requests) in zip(plan['steps'][1:], MODEL_STEPS):
        require((step['name'], step['head'], step['suffix'], step['profile_input_tokens'],
                 step['http_requests'], step['http_loads'], step['timeout']) ==
                (name, 'subset', suffix, tokens, requests, 1, 900), 'model order/configuration changed')
        expected = [('context-8192', 8170)]
        if requests == 2:
            expected.append(('context-204800', 204781))
        require([(c['id'], c['input_tokens']) for c in step['cases']] == expected and
                all(c['repeats'] == 1 and c['output_tokens'] == 256 and
                    c['role'] == 'development_diagnostic' for c in step['cases']),
                'request/predecessor scope changed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=BASE / 'diagnostic-v1')
    parser.add_argument('--capability-review', type=Path, default=BASE / 'interactive-capability.json')
    parser.add_argument('--extra-file', type=Path, action='append', default=[])
    args = parser.parse_args()
    stage, capability = args.output.resolve(), args.capability_review.resolve()
    require(stage.parent == BASE and not stage.exists(), 'require a new stage directly in BASE')
    require(capability == BASE / 'interactive-capability.json' and DOC.is_file(),
            'fixed capability review and stage document required')
    old, audit, snapshot_files, parent_paths = check_parent()
    differences = check_live_dependencies(old)
    cpu, cpu_paths = check_cpu_reuse(old)
    review, tool, tool_paths = check_tool(capability, old)
    check_scope(old)
    source_commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    require(subprocess.run(['git', 'merge-base', '--is-ancestor', old['source_commit'], source_commit],
                           cwd=ROOT, check=False).returncode == 0,
            'source does not contain parent authorized entry')
    paths = set(map(Path, old['files'])) | parent_paths | cpu_paths | tool_paths | {SELF, DOC}
    for extra in args.extra_file:
        path = extra.resolve()
        require(path.is_file() and not path.is_relative_to(stage), 'unsafe explicit extra input')
        paths.add(path)
    # Case files keep their already sealed absolute paths; no prompt copy needed.
    for step in old['steps']:
        for case in step['cases']:
            path = Path(case['path'])
            require(str(path) in old['files'] and sha(path) == old['files'][str(path)],
                    'reused request fixture changed')
            paths.add(path)
    stdout_stat = os.fstat(sys.stdout.fileno())
    for path in paths:
        require(not path.is_relative_to(stage), 'new output cannot already be a frozen input')
        st = path.stat()
        require((st.st_dev, st.st_ino) != (stdout_stat.st_dev, stdout_stat.st_ino),
                'refuse freezing active preparation stdout')
    stage.mkdir(parents=True)
    overlay = stage / 'prepare-overlay'; overlay.mkdir()
    mapping_path = stage / 'source-snapshot-reuse.json'
    save(mapping_path, dict(parent_plan=str(PRIOR_STAGE / 'frozen-plan.json'),
        parent_plan_sha256=PLAN_SHA, audit_path=str(PRIOR / 'audit-delivery.json'),
        audit_sha256=AUDIT_SHA, source_snapshot=old['source_snapshot'], files=snapshot_files,
        worktree_patch_sha256=audit['identities']['source_snapshot']['worktree_patch_sha256'],
        scope='Historical immutable source snapshot reused by identity; no new venv/source copies.'))
    overlay_sources = {SELF, DOC} | {Path(row['path']) for row in differences}
    overlay_mapping = {}
    for path in sorted(overlay_sources):
        target = overlay / path.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        before = sha(path); shutil.copy2(path, target)
        require(sha(target) == before == sha(path), 'preparation overlay source changed')
        overlay_mapping[str(path)] = dict(copy=str(target), sha256=before,
            role='preparation_only' if path == SELF else 'documentation_only')
        paths.add(target)
    patch_path = overlay / 'worktree.patch'
    patch_path.write_bytes(subprocess.check_output(['git', 'diff', 'HEAD'], cwd=ROOT))
    overlay_manifest = stage / 'prepare-overlay.json'
    save(overlay_manifest, dict(files=overlay_mapping, worktree_patch_sha256=sha(patch_path),
        scope='Preparation/documentation delta only; no inference or collector execution-source changes.'))
    paths |= {mapping_path, overlay_manifest, patch_path}
    plan = copy.deepcopy(old)
    plan.update(version=6,
        classification='One fixed Nsight 2026.5.1.161 dependency qualification; conditional GPU attribution only',
        source_commit=source_commit, output=str(stage / 'run'), lease=str(stage / 'lease'),
        files={}, source_snapshot=old['source_snapshot'],
        source_snapshot_reuse=dict(path=str(mapping_path), sha256=sha(mapping_path)),
        prepare_overlay=dict(path=str(overlay_manifest), sha256=sha(overlay_manifest)),
        preparation_dependency_changes=differences, tool_dependency=tool,
        prior_nsys=copy.deepcopy(old['nsys']),
        prior_aborted_stage=dict(plan=str(PRIOR_STAGE / 'frozen-plan.json'), plan_sha256=PLAN_SHA,
            audit=str(PRIOR / 'audit-delivery.json'), audit_sha256=AUDIT_SHA,
            source_validity='ENV_ABORT', source_state='STOPPED', pooled_new_samples=False),
        cpu32_reuse=dict(path=old['cpu_proof']['path'], sha256=old['cpu_proof']['sha256'],
            tests=32, suite_runs=1, new_test_invocations=0,
            scope='Identical executed runner/interactive and test dependencies; preparer independently reviewed and frozen.'),
        new_stage_cpu_budget=dict(max_test_methods=0, max_suite_runs=0, max_elapsed_seconds=0),
        new_stage_native_budget=dict(max_children=0, max_elapsed_seconds=0),
        first_test='Reuse unchanged CPU32 and prior monitor/native contracts; two new-tool interactive fixtures first, then conditional six HTTP.',
        acceptance_scope='Collector dependency qualification and development GPU diagnostics only; no suffix performance acceptance or deployment.',
        preparation_input_policy='Explicit parent/CPU/package/capability inputs; old snapshot referenced, small preparation overlay only. No BASE recursion.')
    plan['nsys'].update(path=str(CLI), sha256=CLI_SHA, capability_path=str(capability),
        capability_sha256=sha(capability), capabilities=copy.deepcopy(review),
        trace_backend=copy.deepcopy(review['trace_backend']),
        interactive_contract=review['contract'])
    for step in plan['steps']:
        step['command'] = [sys.executable, '-B', str(RUNNER), '--plan',
                           str(stage / 'frozen-plan.json'), '--custom-step', step['name']]
    check_scope(plan)
    for path in sorted(paths):
        plan['files'][str(path)] = sha(path)
    checked_manifest(plan['files'])
    save(stage / 'frozen-plan.json', plan)
    print(json.dumps(dict(plan=str(stage / 'frozen-plan.json'), sha256=sha(stage / 'frozen-plan.json'),
        frozen_files=len(plan['files']), package_files=1765, reused_cpu_methods=cpu['tests'],
        new_test_invocations=0, source_snapshot_reused=old['source_snapshot'],
        overlay_files=len(overlay_mapping), http_requests=6, model_starts=4,
        new_fixture_captures=2, seconds=4800,
        note='Preparation only; no test, profiler, export, GPU, HTTP, build, installation or deployment.'), indent=2))


if __name__ == '__main__':
    main()

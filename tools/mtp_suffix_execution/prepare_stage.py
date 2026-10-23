"""Freeze one observation build and a 49-request stage; never run the model."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools/mtp_suffix_zero_hit'))
import telemetry
sys.path.insert(0, str(Path(__file__).resolve().parent))
from spec import STEPS, validate

REFERENCE = ROOT / '.q4t-work/suffix-admission-20261024'
REFERENCE_SHA = '6a4d3d62c09d814a9342cc698e1523e1029d1d3aa3e36e1b80cf7a625a30b090'


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(path, value):
    with path.open('x') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', type=Path, required=True)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--binary-source-commit', required=True)
    parser.add_argument('--build-dir', type=Path, required=True)
    parser.add_argument('--build-log', type=Path, required=True)
    parser.add_argument('--reuse-review', type=Path, required=True)
    parser.add_argument('--nsys', type=Path, required=True)
    parser.add_argument('--nsys-capability', type=Path, required=True)
    parser.add_argument('--extra-file', type=Path, action='append', default=[])
    parser.add_argument('--port', type=int, default=8758)
    args = parser.parse_args()
    stage, binary, build_dir = args.stage.resolve(), args.binary.resolve(), args.build_dir.resolve()
    if not stage.is_relative_to(ROOT / '.q4t-work') or stage.exists():
        parser.error('use a new stage within this checkout .q4t-work')
    if not binary.is_file() or not binary.is_relative_to(build_dir):
        parser.error('binary must exist within its declared observation build directory')
    prior_path = REFERENCE / 'acceptance-v2/frozen-plan.json'
    prior = json.loads(prior_path.read_text())
    reference_binary = Path(prior['candidate'])
    if sha(reference_binary) != REFERENCE_SHA or binary == reference_binary:
        raise RuntimeError('preserve the exact old 6a program separately from the observation build')
    review_path = args.reuse_review.resolve()
    review = json.loads(review_path.read_text())
    if (review.get('observation_only') is not True or
            review.get('unchanged_math_and_default_off') is not True or
            review.get('reference_binary_sha256') != REFERENCE_SHA or
            review.get('source_commit') != args.binary_source_commit):
        raise RuntimeError('reuse review must explicitly bound observation-only source changes')
    for name, expected in review['files'].items():
        if sha(ROOT / name) != expected:
            raise RuntimeError('reviewed observation source identity changed: ' + name)
    allowed = {str((ROOT / name).resolve()) for name in review['allowed_changed_paths']}
    changed = []
    for name, expected in prior['files'].items():
        path = Path(name)
        actual = sha(path)
        if actual == expected:
            continue
        documentation = path.is_relative_to(ROOT / 'docs') or path.name == 'README.md'
        if not documentation and name not in allowed:
            raise RuntimeError('unreviewed changed reused dependency: ' + name)
        changed.append(dict(path=name, before=expected, after=actual,
                            role='documentation' if documentation else 'reviewed_observation_change'))
    capability_path, nsys = args.nsys_capability.resolve(), args.nsys.resolve()
    capability = json.loads(capability_path.read_text())
    if (capability.get('nsys_sha256') != sha(nsys) or
            Path(capability['nsys_path']).resolve() != nsys or
            '2026.1.3' not in capability['version'] or
            capability.get('cuda_profiler_api_capture') is not True or
            capability.get('cuda_nvtx_trace') is not True or
            capability.get('sqlite_export') is not True):
        raise RuntimeError('frozen local Nsight capabilities do not support this fixed branch')
    stage.mkdir()
    fixtures = stage / 'fixtures'
    fixtures.mkdir()
    source_analysis = REFERENCE / 'analysis.json'
    analysis = json.loads(source_analysis.read_text())
    cases = {}
    for name in ['context-8192', 'context-204800', 'repeat-code', 'repeat-text']:
        original = next(c for c in prior['performance'] + prior['scenarios'] if c['id'] == name)
        item = dict(original)
        source = Path(item['path'])
        path = fixtures / (name + '.jsonl')
        path.write_text(source.read_text().splitlines()[0] + '\n')
        item.update(path=str(path), source_path=str(source), source_sha256=sha(source),
                    role='development_diagnostic', repeats=1)
        cases[name] = item
    save(fixtures / 'manifest.json', list(cases.values()))
    previous = ROOT / '.q4t-work/suffix-zero-hit-20261024'
    fix_path = previous / 'telemetry-fixed-tests.json'
    fix = json.loads(fix_path.read_text())
    if (fix['tool_sha256'] != sha(ROOT / 'tools/mtp_suffix_zero_hit/telemetry.py') or
            fix['test_sha256'] != sha(ROOT / 'tools/mtp_suffix_zero_hit/test_telemetry.py') or
            fix['test_log_sha256'] != sha(previous / 'telemetry-fixed-tests.log') or
            fix['tests_passed'] != 11 or fix['tests_skipped'] != 0):
        raise RuntimeError('reused repaired monitor evidence identity changed')
    source = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    plan = dict(version=1, classification='bounded execution observation; no optimization delivery',
        candidate=str(binary), candidate_sha256=sha(binary),
        candidate_source_commit=args.binary_source_commit, source_commit=source,
        reference_binary=str(reference_binary), reference_binary_sha256=REFERENCE_SHA,
        reference_runtime_source=prior['candidate_source_commit'],
        parent=str(ROOT / 'build/q4t'), parent_sha256=prior['parent_sha256'],
        model=prior['model'], artifact=prior['artifact'],
        subset_manifest_sha256=prior['subset_manifest_sha256'],
        model_file_metadata=prior['model_file_metadata'],
        output=str(stage / 'run'), lease=str(stage / 'lease'), port=args.port,
        http_requests=49, http_loads=13, stage_timeout_seconds=7200,
        direct_contract_timeout_seconds=0, retries=0, warmups=0,
        observation_build_limit=1, runtime_optimization_candidates=0,
        conditional_optimization_stage='NOT_AUTHORIZED_BY_THIS_PLAN',
        deployment='NOT_AUTHORIZED_FOR_OBSERVATION_BUILD',
        offline_analysis='Analysis scripts are sealed separately before post-stage execution; they are not measurement dependencies. Raw evidence remains immutable.',
        resource_sample_interval_seconds=1, resource_sample_max_age_seconds=5,
        cache='natural inherited page cache; startup auto; no warmup/drop/affinity/power changes',
        first_test='normal real evalscope HTTP on observation binary; all observation switches absent',
        stop='first identity/HTTP/output/lifecycle/monitor failure or budget; no retry/profile expansion',
        scope='B1 greedy k3, all observed materials development-only; default full head and explicit subset unchanged',
        telemetry_sources=telemetry.discover_sources(), monitor_fix=fix,
        nsys=dict(path=str(nsys), sha256=sha(nsys), capability_path=str(capability_path),
            capability_sha256=sha(capability_path), capabilities=capability,
            profile_options=['--trace=cuda,nvtx', '--sample=none', '--cpuctxsw=none',
                             '--capture-range=cudaProfilerApi', '--capture-range-end=stop',
                             '--kill=none', '--wait=all'],
            export_timeout_seconds=300, launcher_exit_timeout_seconds=120),
        build=dict(directory=str(build_dir), log=str(args.build_log.resolve()),
                   actual_source_commit=args.binary_source_commit),
        reuse_review=dict(path=str(review_path), sha256=sha(review_path),
                         changed_reused_dependencies=changed, content=review),
        inherited_contracts=dict(path=str(REFERENCE / 'audit-evidence.json'),
            sha256=sha(REFERENCE / 'audit-evidence.json'), quality=22, host=86, stage_suite=10,
            scope='Original 6a mathematical contract evidence under reviewed unchanged computation dependencies only; not new-binary whole-model quality. Two-GDN-layer oracle scope unchanged.'),
        repeat_goal=dict(full='each code/text median ON/OFF >=1.05',
            subset='each B/A1,B/A2 >1; descriptive geometric mean >=1.05 per code/text',
            scope='development-material confirmation only; shared B and within-start repetitions'),
        steps=[], files={})
    runner = ROOT / 'tools/mtp_suffix_execution/run_stage.py'
    for name, head, suffix, role, requested, capture in STEPS:
        chosen = [dict(cases[key], repeats=count) for key, count in requested]
        for case in chosen:
            comparison = analysis['paired_comparisons'][head][case['id']]
            if not comparison['all_blocks_output_identity_equal']:
                raise RuntimeError('reference head output is not stable')
            reference = next(r for r in analysis['protections']['entries']
                if r['head'] == head and r['case'] == case['id'] and r['block'] == 1)
            case['expected_output_sha256'] = reference['off_first_request']['output_sha256']
        observed = role in ('execution_diagnostic', 'nsys_profile')
        plan['steps'].append(dict(name=name, head=head, suffix=suffix,
            measurement_role=role, execution_diagnostic=observed,
            profile_input_tokens=capture, kind='timing' if observed else 'block',
            cases=chosen, block=None, timeout=1200 if capture else 900,
            http_loads=1, http_requests=sum(c['repeats'] for c in chosen),
            command=[sys.executable, '-B', str(runner), '--plan', str(stage / 'frozen-plan.json'),
                     '--custom-step', name]))
    validate(plan)
    paths = set(map(Path, prior['files']))
    paths |= {prior_path, source_analysis, REFERENCE / 'audit-evidence.json', binary,
              args.build_log.resolve(), review_path, capability_path, nsys, fix_path,
              previous / 'telemetry-fixed-tests.log', ROOT / 'CMakeLists.txt',
              ROOT / 'docs/STATUS.md', ROOT / 'docs/EVALUATION.md',
              ROOT / 'docs/PERFORMANCE_DECISION_PROTOCOL.md'}
    for name in ['spec.py', 'prepare_stage.py', 'run_stage.py', 'README.md']:
        paths.add(ROOT / 'tools/mtp_suffix_execution' / name)
    for directory in ['src', 'include', 'cmake', 'tools/mtp_suffix_zero_hit']:
        paths |= {p for p in (ROOT / directory).rglob('*') if p.is_file() and '__pycache__' not in p.parts}
    metadata = {p for p in build_dir.rglob('*') if p.is_file() and (
        p.suffix == '.cmake' or p.name in ('CMakeCache.txt', 'compile_commands.json',
        'flags.make', 'link.txt', 'build.ninja', 'rules.ninja', 'Makefile', 'build.make'))}
    if build_dir / 'CMakeCache.txt' not in metadata:
        raise RuntimeError('observation build has no CMakeCache identity')
    paths |= metadata | set(fixtures.glob('*')) | {p.resolve() for p in args.extra_file}
    for path in (Path(str(binary) + '.release.json'),
                 Path(str(binary) + '.sha256'), build_dir / 'build-metadata.json'):
        if path.is_file():
            paths.add(path)
    for name in capability.get('evidence_files', []):
        paths.add(Path(name).resolve())
    plan['build']['metadata_files'] = {str(p): sha(p) for p in sorted(metadata)}
    snapshot = stage / 'source-snapshot'
    snapshot.mkdir()
    for path in sorted(paths):
        plan['files'][str(path)] = sha(path)
        if path.is_relative_to(ROOT) and '.q4t-work' not in path.relative_to(ROOT).parts:
            target = snapshot / path.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    (snapshot / 'worktree.patch').write_bytes(subprocess.check_output(['git', 'diff', 'HEAD'], cwd=ROOT))
    plan['source_snapshot'] = str(snapshot)
    save(stage / 'frozen-plan.json', plan)
    print(json.dumps(dict(plan=str(stage / 'frozen-plan.json'), sha256=sha(stage / 'frozen-plan.json'),
                         requests=49, starts=13, candidate=plan['candidate_sha256']), indent=2))


if __name__ == '__main__':
    main()

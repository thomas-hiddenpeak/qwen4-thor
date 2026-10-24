"""Freeze four collector controls and six conditional HTTP requests, no runs."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / '.q4t-work/suffix-nsys-20261024'
PRIOR = ROOT / '.q4t-work/suffix-execution-rerun-20261024'
STAGE = BASE / 'diagnostic-v1'
PRIOR_SHA = 'f999e0950cd14dd364e4cb249c1e58184cf229887380d38a6ca0a903c8db69e1'
BINARY_SHA = 'f0ff0fe8e11791d561bc49f4dc42762415d4ff000836bbd8c38548723906c914'


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(path, value):
    with path.open('x') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def main():
    prior_path = PRIOR / 'diagnostic-v1/frozen-plan.json'
    if sha(prior_path) != PRIOR_SHA:
        raise RuntimeError('previous frozen plan changed')
    old = json.loads(prior_path.read_text())
    reused = {
        'audit-rerun-v2.json': 'fbf7f43cadcb645d73abb403117c1ae9a3c028fc7cb0e59f3ccee0ea5100fb03',
        'normal-cpu-summary.json': '5084e200b7069f7bd340031db735cc3576fbfd848934f65e38e65c0a99420f22',
        'monitor-cpu-proof.json': 'ed7d1451af7a5e7ab20cb36e0628c94ba2c8ceafaaef35e4d828d63ead880d75',
    }
    for name, expected in reused.items():
        if sha(PRIOR / name) != expected:
            raise RuntimeError('reused proof changed: ' + name)
    changes = []
    for name, before in old['files'].items():
        path = Path(name)
        after = sha(path)
        if after != before:
            if not path.is_relative_to(ROOT / 'docs') and path.name not in (
                    'README.md', 'README-rerun.md'):
                raise RuntimeError('reused executable dependency changed: ' + name)
            changes.append(dict(path=name, before=before, after=after,
                                role='documentation'))
    if sha(Path(old['candidate'])) != BINARY_SHA:
        raise RuntimeError('observation binary changed')
    for path, expected in old['reuse_review']['content']['files'].items():
        if sha(ROOT / path) != expected:
            raise RuntimeError('observation runtime source changed: ' + path)
    proof_path = BASE / 'cpu-proof.json'
    proof = json.loads(proof_path.read_text())
    if (proof['failed'] or proof['errors'] or proof['skipped'] or
            not 0 < proof['tests'] <= 30 or not 0 < proof['suite_runs'] <= 2 or
            not 0 <= proof['elapsed_seconds'] <= 180):
        raise RuntimeError('bounded CPU proof failed')
    required_proof = {str(p.resolve()) for p in Path(__file__).resolve().parent.glob('*.py')}
    required_proof.add(str((BASE / 'cpu-tests.log').resolve()))
    if not required_proof <= proof['files'].keys():
        raise RuntimeError('CPU proof omits executed tools/tests/log')
    for name, expected in proof['files'].items():
        if sha(Path(name)) != expected:
            raise RuntimeError('CPU verified tool changed: ' + name)
    fixture = BASE / 'build-memset/fixture'
    build_log = BASE / 'build-memset.log'
    if not fixture.is_file() or not build_log.is_file():
        raise RuntimeError('missing fixture build')
    if 'warning:' in build_log.read_text().lower():
        raise RuntimeError('fixture build warnings')
    build_exit = json.loads((BASE / 'build-memset-exit.json').read_text())
    if (build_exit['returncode'] != 0 or build_exit['binary_sha256'] != sha(fixture) or
            build_exit['source_sha256'] != sha(Path(__file__).with_name('fixture.cu'))):
        raise RuntimeError('fixture build/source receipt differs')
    STAGE.mkdir()
    runner = ROOT / 'tools/mtp_suffix_nsys/run_stage.py'
    plan = copy.deepcopy(old)
    plan.update(version=3, classification='collector bug controls and bounded GPU attribution; no runtime optimization',
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        output=str(STAGE / 'run'), lease=str(STAGE / 'lease'),
        source_snapshot=str(STAGE / 'source-snapshot'), files={},
        http_requests=6, http_loads=4, stage_timeout_seconds=5400,
        direct_contract_timeout_seconds=1200, retries=0, warmups=0,
        observation_build_limit=0, runtime_optimization_candidates=0,
        fixture_binary=str(fixture), fixture_binary_sha256=sha(fixture),
        fixture_contract=dict(iterations=32, kernels=32, h2d=32, d2h=32,
            nvtx_ranges=33, memset_apis=32, memset_bytes_even=512,
            memset_bytes_odd=16384, unknown_gpu_memory_coverage_blocks_model=True),
        fixture_timeout_seconds=300, fixture_nvlog_limit_bytes=64 * 1024**2,
        fixture_variants=[dict(name='direct-stop', wrapper=False, external=False),
                          dict(name='wrapper-stop', wrapper=True, external=False),
                          dict(name='direct-external', wrapper=False, external=True),
                          dict(name='wrapper-external', wrapper=True, external=True)],
        fixture_log_config='+ 100iI 100wW 100eE 100fF global\n+ 100iI 100wW 100eE 100fF Cupti_Public\n$ {log}\nForceFlush\nFormat $sevc$time| ${{name:0}} | $proc pid=$pid tid=${{tid:8}} | ${{file:0}}:${{line:0}} [${{sfunc:0}}]: $text\n',
        first_test='Targeted collector CPU/CUDA controls; prior passed normal HTTP dependency evidence reused.',
        stop='First known-work/negative-control/identity/HTTP/monitor failure. Fixture warnings documented; only wrapper-external with strict actual-target diagnostics may authorize model phase. No retries.',
        prior_dependency_changes=changes,
        cpu_proof=dict(path=str(proof_path), sha256=sha(proof_path)),
        reuse=dict(prior_plan=str(prior_path), prior_plan_sha256=PRIOR_SHA,
            normal_http_requests=37, cpu_http_requests=6, monitor_cpu_tests=39,
            repeat_full_code_percent=13.6774, repeat_full_text_percent=14.4034,
            repeat_subset_code_shared_b_gm_percent=6.5335,
            repeat_subset_text_shared_b_gm_percent=6.7304,
            pooled_new_samples=False, scope='Same unchanged executable and dependencies; descriptive development evidence, not five-tier acceptance.'),
        steps=[])
    options = ['--trace=cuda,nvtx', '--sample=none', '--cpuctxsw=none',
               '--cuda-trace-scope=process-tree', '--capture-range=cudaProfilerApi',
               '--capture-range-end=none', '--flush-on-cudaprofilerstop=true',
               '--kill=none', '--wait=all']
    plan['nsys'].update(profile_options=options, export_timeout_seconds=180,
                        launcher_exit_timeout_seconds=120)
    plan['steps'].append(dict(name='fixture-controls', measurement_role='collector_contract',
        execution_diagnostic=False, kind='contract', cases=[], http_loads=0,
        http_requests=0, timeout=1200, profile_input_tokens=None))
    fixtures = STAGE / 'fixtures'
    fixtures.mkdir()
    for name in ('nsys8-off', 'nsys8-on', 'nsys200-on', 'nsys200-off'):
        step = copy.deepcopy(next(s for s in old['steps'] if s['name'] == name))
        step['timeout'] = 900
        for case in step['cases']:
            source = Path(case['path'])
            target = fixtures / source.name
            if not target.exists():
                shutil.copy2(source, target)
            case['path'] = str(target)
        plan['steps'].append(step)
    for step in plan['steps']:
        step['command'] = [sys.executable, '-B', str(runner), '--plan',
                           str(STAGE / 'frozen-plan.json'), '--custom-step', step['name']]
    paths = set(map(Path, old['files'])) | set(fixtures.glob('*'))
    paths |= {prior_path, PRIOR / 'audit-rerun-v2.json', PRIOR / 'normal-cpu-summary.json',
              PRIOR / 'normal-cpu-analysis.md', PRIOR / 'monitor-cpu-proof.json',
              PRIOR / 'diagnostic-v1/run/files.json',
              fixture, build_log, proof_path,
              ROOT / 'docs/MTP_SUFFIX_NSYS_2026-10-24.md'}
    paths |= {p for p in Path(__file__).resolve().parent.rglob('*')
              if p.is_file() and '__pycache__' not in p.parts}
    paths |= {p for p in (BASE / 'references').rglob('*') if p.is_file()}
    paths |= {p for p in (BASE / 'initial-fixture-build').rglob('*') if p.is_file()}
    paths |= {p for p in BASE.glob('*') if p.is_file() and p.suffix in ('.json', '.md', '.log')}
    # Freeze the real Nsight executable and injection/CUPTI identities, not only
    # the toolkit launcher script. The static review enumerated these paths.
    review = json.loads((BASE / 'nsys-mechanism-review.json').read_text())
    for name, expected in review['installed_files'].items():
        if sha(Path(name)) != expected:
            raise RuntimeError('reviewed Nsight dependency changed: ' + name)
    for item in review['references']:
        if 'path' in item and sha(Path(item['path'])) != item['sha256']:
            raise RuntimeError('reviewed reference changed: ' + item['path'])
    def collect_files(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(key, str) and key.startswith('/') and Path(key).is_file():
                    paths.add(Path(key))
                collect_files(item)
        elif isinstance(value, list):
            for item in value:
                collect_files(item)
        elif isinstance(value, str) and value.startswith('/') and Path(value).is_file():
            paths.add(Path(value))
    collect_files(review)
    snapshot = Path(plan['source_snapshot'])
    snapshot.mkdir()
    for path in sorted(paths):
        plan['files'][str(path)] = sha(path)
        if path.is_relative_to(ROOT) and '.q4t-work' not in path.relative_to(ROOT).parts:
            target = snapshot / path.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    (snapshot / 'worktree.patch').write_bytes(subprocess.check_output(['git', 'diff', 'HEAD'], cwd=ROOT))
    save(STAGE / 'frozen-plan.json', plan)
    print(json.dumps(dict(plan=str(STAGE / 'frozen-plan.json'),
        sha256=sha(STAGE / 'frozen-plan.json'), files=len(plan['files']),
        http=6, starts=4, captures_max=8, seconds=5400), indent=2))


if __name__ == '__main__':
    main()

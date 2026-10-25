"""Freeze one conditional actual-model parameter observation, no profiler scan."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT/'.q4t-work/suffix-model-ledger-20261026'
PARENT = ROOT/'.q4t-work/suffix-report-binding-20261026/diagnostic-v1/frozen-plan.json'


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def load(path):
    return json.loads(Path(path).read_text())


def require(value, message):
    if not value:
        raise RuntimeError(message)


def main():
    require(not (BASE/'frozen-plan.json').exists(), 'one preparation only')
    scope = load(BASE/'scope.json')
    require(time.time()+1080 < scope['created_unix']+3600,
            'insufficient frozen wall for the bounded stage and cleanup')
    require(all(sha(p) == v for p, v in scope['frozen_inputs'].items()),
            'scope input changed')
    parent = load(PARENT)
    require(sha(PARENT) == 'c71846e9741c69ae86569de58e4c42dcef0437e7093bb5762a1f67cc00297892',
            'model/case reference plan changed')
    cpu = load(BASE/'cpu-proof.json')
    require(set(cpu['test_modules']) == {'test_ledger_analyze.py', 'test_ledger_stage.py'} and
            10 <= cpu['tests'] <= 32 and cpu['tests'] == sum(cpu['test_modules'].values()) and
            cpu['suite_runs'] <= 2 and cpu['elapsed_seconds'] <= 120 and
            not any(cpu[k] for k in ('failed', 'errors', 'skipped')),
            'affected CPU contract failed')
    require(all(sha(p) == v for p, v in cpu['files'].items()), 'tested source drift')
    build = load(BASE/'build-proof.json')
    require(build['returncode'] == 0 and build['failure'] is None and not build['warnings'] and
            build['total_elapsed_seconds'] <= 300 and
            sha(build['binary']) == build['binary_sha256'], 'shared-library build invalid')
    require(all(sha(p) == v for p, v in build['files'].items()), 'compiled input drift')
    fixture = ROOT/'.q4t-work/suffix-memset-link-20261026/build/mechanism_probe'
    require(sha(fixture) == '16b3303c505ea9fabe82e3289f6f647dbfd533b13da4b049d4a7aa035fec63b3',
            'known-work fixture changed')
    files = dict(scope['frozen_inputs']) | dict(cpu['files']) | dict(build['files'])
    # Reuse the exact live client environment and subset artifacts. The older
    # source snapshot remains a historical build anchor, not current tool code.
    prefixes = (str(ROOT/'tools/evalscope/.venv')+'/', parent['artifact']+'/')
    for path, digest in parent['files'].items():
        if path.startswith(prefixes):
            require(sha(path) == digest, 'client/artifact dependency drift: '+path)
            files[path] = digest
    for name in ('candidate', 'reference_binary'):
        path = parent[name]
        require(sha(path) == parent[name+'_sha256'], 'runtime/reference identity changed')
        files[path] = sha(path)
    selected = {Path(p) for p in files}
    selected.update((BASE/'scope.json', BASE/'build-proof.json', BASE/'cpu-proof.json',
        Path(__file__), ROOT/'AGENTS.md', ROOT/'docs/EVALUATION.md',
        ROOT/'docs/PERFORMANCE_DECISION_PROTOCOL.md'))
    selected.update((ROOT/'tools/mtp_suffix_nsys').glob('*.py'))
    monitor_fix = parent['monitor_fix']
    for key in ('cpu_proof', 'native_proof', 'kernel_contract', 'runner_cpu_proof'):
        reference = monitor_fix[key]
        require(sha(reference['path']) == reference['sha256'], 'monitor evidence drift: '+key)
        selected.add(Path(reference['path']))
    kernel = load(monitor_fix['kernel_contract']['path'])
    for key, digest in (('original_path', 'sha256'), ('copy_path', 'copy_sha256')):
        require(sha(kernel[key]) == kernel[digest], 'monitor kernel contract drift')
        selected.add(Path(kernel[key]))
    require(sha(ROOT/'tools/mtp_suffix_nsys/run_stage.py') == monitor_fix['runner_sha256'],
            'reused monitor runner identity changed')
    # ldd only inspects dynamic dependencies; it does not run the model or GPU.
    for label, executable in (('model', parent['candidate']), ('fixture', str(fixture))):
        result = subprocess.run(['ldd', executable], text=True, capture_output=True,
                                check=True, timeout=30)
        require(not result.stderr and 'not found' not in result.stdout, 'unresolved '+label+' library')
        path = BASE/(label+'-ldd.txt')
        with path.open('x') as stream:
            stream.write(result.stdout)
        selected.add(path)
        for line in result.stdout.splitlines():
            match = re.search(r'(?:=>\s*)?(/\S+)\s+\(', line)
            if match:
                selected.add(Path(match[1]).resolve())
    selected.add(Path('/usr/lib/aarch64-linux-gnu/libcuda.so.1').resolve())
    # Guard's NVML library is a CPU-side monitoring dependency.
    nvml = Path('/usr/lib/aarch64-linux-gnu/libnvidia-ml.so.1')
    if nvml.exists():
        selected.add(nvml.resolve())
    for path, expected in parent['model_file_metadata'].items():
        st = Path(path).stat()
        require(dict(size=st.st_size, mtime_ns=st.st_mtime_ns, device=st.st_dev,
                     inode=st.st_ino) == expected, 'model metadata changed')
    plan_path = BASE/'frozen-plan.json'
    step = dict(parent['steps'][1], name='model8-off', measurement_role='cupti_parameters',
                timeout=600, command=[])
    fixture_step = dict(name='fixture-ledger', kind='contract', measurement_role='ledger_fixture',
        http_loads=0, http_requests=0, timeout=120, execution_diagnostic=False, cases=[])
    for item in (fixture_step, step):
        item['command'] = ['/usr/bin/python3', '-B',
            str(ROOT/'tools/mtp_suffix_nsys/ledger_stage.py'), '--plan', str(plan_path),
            '--custom-step', item['name']]
    require(step['cases'][0]['input_tokens'] == 8170 and step['cases'][0]['repeats'] == 1 and
            step['cases'][0]['expected_output_sha256'] == scope['model']['expected_output_sha256'],
            'single original request changed')
    for case in step['cases']:
        selected.add(Path(case['path']))
    inherited = ('candidate', 'candidate_sha256', 'candidate_source_commit', 'reference_binary',
        'reference_binary_sha256', 'reference_runtime_source', 'model', 'artifact',
        'subset_manifest_sha256', 'model_file_metadata', 'telemetry_sources', 'cache')
    plan = {k: parent[k] for k in inherited}
    plan.update(version=1, classification=scope['classification'], source_commit=scope['source_commit'],
        scope=str(BASE/'scope.json'), scope_sha256=sha(BASE/'scope.json'), output=str(BASE/'run'),
        lease=str(BASE/'lease'), port=8758, http_requests=1, http_loads=1,
        stage_timeout_seconds=900, direct_contract_timeout_seconds=120,
        resource_sample_interval_seconds=1, resource_sample_max_age_seconds=5,
        monitor_fix=monitor_fix,
        retries=0, warmups=0, nsys_processes=0, runtime_optimization_candidates=0,
        expected_work=dict(t4=74, t1=3), steps=[fixture_step, step],
        ledger_library=build['binary'], ledger_library_sha256=build['binary_sha256'],
        fixture_binary=str(fixture), fixture_binary_sha256=sha(fixture),
        parent=str(PARENT), parent_sha256=sha(PARENT),
        source_snapshot_reuse=dict(path=parent['source_snapshot'],
            scope='Historical unchanged runtime build anchor only, not current diagnostic source'),
        normal_performance_claim=False, model_qualification=False, deployment=False,
        cpu_proof=str(BASE/'cpu-proof.json'), build_proof=str(BASE/'build-proof.json'),
        files={str(p):sha(p) for p in sorted(selected)})
    with plan_path.open('x') as stream:
        json.dump(plan, stream, indent=2)
        stream.write('\n')
    print(json.dumps(dict(plan=str(plan_path), sha256=sha(plan_path), files=len(plan['files']),
                         model_metadata=len(plan['model_file_metadata']))))


if __name__ == '__main__':
    main()

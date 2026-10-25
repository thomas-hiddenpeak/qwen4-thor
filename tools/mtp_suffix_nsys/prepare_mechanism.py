"""Freeze the single fixed-shape diagnostic after bounded build/CPU checks."""
import hashlib
import json
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / '.q4t-work/suffix-memset-parameter-20261026'
PARENT = ROOT / '.q4t-work/suffix-report-binding-20261026/diagnostic-v1/frozen-plan.json'
PARENT_SHA = 'c71846e9741c69ae86569de58e4c42dcef0437e7093bb5762a1f67cc00297892'


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def load(path):
    return json.loads(Path(path).read_text())


def require(value, reason):
    if not value:
        raise RuntimeError(reason)


def main():
    require(not (BASE/'frozen-plan.json').exists(), 'one plan only')
    scope = load(BASE/'scope.json')
    require(time.time() < scope['created_unix']+1800, 'work budget exhausted')
    require(sha(PARENT) == PARENT_SHA, 'parent plan changed')
    parent = load(PARENT)
    cpu = load(BASE/'cpu-proof.json')
    modules = {'test_run_stage.py': 19, 'test_synchronous.py': 15,
               'test_nvlog_snapshot.py': 8, 'test_mechanism_analyze.py': 8,
               'test_mechanism_stage.py': 5}
    require(cpu['test_modules'] == modules and cpu['tests'] == sum(modules.values()) and
            cpu['suite_runs'] in (1, 2) and cpu['elapsed_seconds'] <= 120 and
            not any(cpu[k] for k in ('failed', 'errors', 'skipped')), 'CPU contracts failed')
    require(all(str(ROOT/'tools/mtp_suffix_nsys'/name) in cpu['files']
                for name in (*modules, 'mechanism_stage.py', 'mechanism_analyze.py',
                             'nvlog_snapshot.py', 'synchronous.py')), 'CPU source coverage missing')
    require(all(sha(p) == v for p, v in cpu['files'].items()), 'CPU source/evidence drift')
    build = load(BASE/'build-proof.json')
    require(build['returncode'] == 0 and not build['warnings'] and
            build['elapsed_seconds'] <= 300, 'build did not meet its contract')
    binary = Path(build['binary'])
    require(binary.is_relative_to(BASE/'build') and sha(binary) == build['binary_sha256'],
            'probe binary identity changed')
    tracked = subprocess.check_output(['git', 'ls-files', 'tools', 'src', 'include'],
                                     cwd=ROOT, text=True).splitlines()
    sources = {ROOT/n for n in tracked if Path(n).suffix in ('.py', '.cu', '.cpp', '.h', '.cuh')}
    sources.update((ROOT/'tools/mtp_suffix_nsys').glob('*.py'))
    sources.add(ROOT/'tools/mtp_suffix_nsys/mechanism_probe.cu')
    sources.update(Path(p) for p in cpu['files'])
    sources.update(Path(p) for p in build['files'])
    sources.update((BASE/'scope.json', BASE/'cpu-proof.json', BASE/'build-proof.json', PARENT))
    tool = parent['tool_dependency']
    sources.update((Path(tool['path']), Path(tool['manifest'])))
    baseline = {'build/q4t': 'f356b2c96330101abf1e5fa3fced495a37f4ea5a270ed4457037e0d55735d313',
        'build/q4t.release.json': '422993b0aba5d21462ac3a7e44a78cbb8fb1721b41ca961bd476c679581e7e96',
        '.q4t-work/sha2-startup-20261023/delivery-package/artifact/manifest.json':
        'b63b0f251fddc8a75749a54dd4b22477fdb8da875cc26f83595534c71062c3ce'}
    require(all(sha(ROOT/p) == v for p, v in baseline.items()), 'delivered baseline changed')
    sources.update(ROOT/p for p in baseline)
    sources.add(Path('/usr/lib/aarch64-linux-gnu/libcuda.so.1').resolve())
    require(sha(tool['manifest']) == tool['manifest_sha256'] and
            sha(tool['path']) == tool['sha256'], 'tool dependency identity changed')
    for path, digest in build['files'].items():
        require(sha(path) == digest, 'compiled source/dependency changed: '+path)
    plan = dict(schema=1, created_unix=time.time(), source_commit=scope['source_commit'],
        scope=str(BASE/'scope.json'), output=str(BASE/'run'), lease=str(BASE/'lease'),
        lock_path=str(ROOT/'.q4t-work/gpu-exclusive.lock'), binary=str(binary),
        binary_sha256=sha(binary), tool_dependency=tool, nvlog_config=parent['fixture_log_config'],
        cpu_tests=cpu['tests'], normal_performance_claim=False, model_qualification=False,
        no_cross_process_correlation_join=True, files={str(p): sha(p) for p in sorted(sources)})
    with (BASE/'frozen-plan.json').open('x') as stream:
        json.dump(plan, stream, indent=2)
        stream.write('\n')
    print(json.dumps(dict(plan=str(BASE/'frozen-plan.json'), sha256=sha(BASE/'frozen-plan.json'),
                         files=len(plan['files']), binary_sha256=sha(binary)), indent=2))


if __name__ == '__main__':
    main()

"""Build and seal an independent candidate; never execute CUDA or deploy."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parents[3]
BASE = ROOT / '.q4t-work/startup-plan-20261020'
DEFAULT_SHA = '19ea4e57bf3a0e4f568a3e59c31ccbb4e71ad535db997682d8160d4be2f9c469'


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def identity(path):
    return dict(path=str(path), sha256=sha(path), bytes=path.stat().st_size)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=BASE)
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / '.q4t-work'):
        parser.error('output must be inside .q4t-work')
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / 'build.json'
    if manifest_path.exists():
        parser.error('refusing to replace a sealed build manifest')
    default = ROOT / 'build/q4t'
    if sha(default) != DEFAULT_SHA:
        raise RuntimeError('default binary changed')
    sources = [ROOT / 'CMakeLists.txt', Path(__file__).resolve(),
               ROOT / 'tools/verify/startup_plan/reservation.cpp',
               ROOT / 'tools/verify/device_reserve/owner.h']
    for directory in ('include', 'src'):
        sources.extend(p for p in (ROOT / directory).rglob('*') if p.is_file())
    inputs = {str(p): identity(p) for p in sorted(set(sources))}
    build = output / 'build'
    commands = [
        ['cmake', '-S', str(ROOT), '-B', str(build),
         '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_CXX_COMPILER=g++-14',
         '-DCMAKE_CUDA_HOST_COMPILER=g++-14', '-DQ4T_CUDA_ARCHITECTURES=110a',
         '-DQ4T_STARTUP_RESOURCE_PLAN=ON'],
        ['cmake', '--build', str(build), '--parallel', '6', '--target', 'q4t'],
    ]
    attempt = time.time_ns()
    logs = []
    for i, command in enumerate(commands):
        log = output / f'build-{attempt}-{i}.log'
        logs.append(log)
        with log.open('x') as stream:
            result = subprocess.run(command, stdout=stream,
                                    stderr=subprocess.STDOUT, check=False)
        result.check_returncode()
        if re.search(r'\bwarning:', log.read_text(), re.IGNORECASE):
            raise RuntimeError('compiler warning: ' + str(log))
    for path, record in inputs.items():
        if sha(Path(path)) != record['sha256']:
            raise RuntimeError('source changed during build: ' + path)
    frozen = output / f'source-{attempt}'
    for path in sources:
        destination = frozen / path.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        if sha(destination) != inputs[str(path)]['sha256']:
            raise RuntimeError('source snapshot mismatch')
    link_inputs = {}
    dependency = build / 'CMakeFiles/q4t.dir/link.d'
    rule = dependency.read_text().replace('\\\n', ' ').splitlines()[0]
    for token in shlex.split(rule.split(':', 1)[1]):
        path = Path(token)
        path = (path if path.is_absolute() else build / path).resolve()
        link_inputs[str(path)] = identity(path)
    candidate = identity(build / 'q4t')
    if sha(default) != DEFAULT_SHA:
        raise RuntimeError('default changed during build')
    manifest = dict(status='COMPLETED', candidate=candidate,
                    candidate_sha256=candidate['sha256'],
                    default_sha256=DEFAULT_SHA, default_replacement=False,
                    real_build_cache=identity(build / 'CMakeCache.txt'),
                    source_and_plan_inputs=inputs, link_inputs=link_inputs,
                    source_snapshot=str(frozen), commands=commands,
                    logs=[identity(log) for log in logs], gpu_execution=False)
    with manifest_path.open('x') as stream:
        json.dump(manifest, stream, indent=2)
        stream.write('\n')
    print(json.dumps(dict(status='COMPLETED', candidate=candidate)))


if __name__ == '__main__':
    main()

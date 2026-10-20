"""Reproduce the default binary, then link a separate reservation prototype.

Only host compilation/linking occurs here. Nothing executes CUDA or replaces
the default binary. Failed output is retained; use a fresh output to retry.
"""
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
HERE = Path(__file__).resolve().parent
BASE = ROOT / '.q4t-work/device-reserve-20261020'
BUILD = ROOT / '.q4t-work/offload-foundation-20261020/build'
DEFAULT_SHA = '19ea4e57bf3a0e4f568a3e59c31ccbb4e71ad535db997682d8160d4be2f9c469'
APIS = ('cudaMalloc', 'cudaFree', 'cudaHostAlloc', 'cudaMallocHost',
        'cudaFreeHost', 'cudaMallocAsync', 'cudaFreeAsync',
        'cudaStreamSynchronize', 'cudaDeviceSynchronize', 'cudaEventRecord',
        'cudaEventSynchronize')
CUDA = Path('/usr/local/cuda-13.3/targets/sbsa-linux')


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def save(path, value):
    with path.open('x') as output:
        json.dump(value, output, indent=2)
        output.write('\n')


def file_identity(path):
    return dict(path=str(path), sha256=sha(path), bytes=path.stat().st_size)


def run(command, cwd, log):
    with log.open('x') as output:
        result = subprocess.run(command, cwd=cwd, stdout=output,
                                stderr=subprocess.STDOUT, check=False)
    if result.returncode:
        raise RuntimeError(f'host build failed ({result.returncode}); see {log}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan-dir', type=Path, default=BASE / 'plan')
    parser.add_argument('--output', type=Path, default=BASE)
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / '.q4t-work'):
        parser.error('--output must be inside this repository .q4t-work')
    output.mkdir(parents=True, exist_ok=True)
    for name in ('build.json', 'build-failure.json', 'unwrapped',
                 'unwrapped-link.log', 'candidate'):
        if (output / name).exists():
            parser.error('refusing to overwrite build evidence: ' + str(output / name))
    plan = args.plan_dir.resolve(strict=True)
    plan_json = plan / 'plan.json'
    plan_header = plan / 'reserve_plan.h'
    plan_sha = sha(plan_json)
    header = plan_header.read_text()
    match = re.search(r'kPlanSha256\[\]\s*=\s*"([0-9a-f]{64})"', header)
    if not match or match.group(1) != plan_sha:
        parser.error('generated header kPlanSha256 does not match plan.json')
    default = ROOT / 'build/q4t'
    if sha(default) != DEFAULT_SHA:
        parser.error('default binary identity changed; refusing prototype build')
    link_file = BUILD / 'CMakeFiles/q4t.dir/link.txt'
    dependency_file = BUILD / 'CMakeFiles/q4t.dir/link.d'
    cache_file = BUILD / 'CMakeCache.txt'
    source_paths = [HERE / 'memory_trace.cpp', HERE / 'owner.h',
                    HERE / 'gpu_contract.cpp', Path(__file__)]
    input_paths = source_paths + [plan_json, plan_header, link_file,
                                 dependency_file, cache_file]
    originals = {str(path): file_identity(path) for path in input_paths}
    link = [token for token in shlex.split(link_file.read_text())
            if not token.startswith('-Wl,--dependency-file=')]
    if link.count('-o') != 1:
        parser.error('expected exactly one output in authentic link command')
    link[link.index('-o') + 1] = str(output / 'unwrapped')
    link_inputs = {}
    for token in link:
        if token.startswith('-'):
            continue
        path = Path(token)
        path = path if path.is_absolute() else BUILD / path
        if path.is_file() and path.suffix in ('.o', '.a', '.so'):
            link_inputs[str(path.resolve())] = file_identity(path.resolve())
    # Include CRT objects, -l archives and transitive system libraries recorded
    # by the original linker, as well as the explicit command-line inputs.
    dependency_rule = dependency_file.read_text().replace('\\\n', ' ').splitlines()[0]
    for token in shlex.split(dependency_rule.split(':', 1)[1]):
        path = Path(token)
        path = (path if path.is_absolute() else BUILD / path).resolve(strict=True)
        link_inputs[str(path)] = file_identity(path)
    manifest = dict(
        status='BUILDING', started=time.time(), default_sha256=DEFAULT_SHA,
        expected_source_build=str(BUILD),
        source_and_plan_inputs=originals, link_inputs=link_inputs,
        plan=dict(path=str(plan_json), sha256=plan_sha,
                  header=file_identity(plan_header), data=json.loads(plan_json.read_text())),
        wrapped_apis=list(APIS), unwrapped_link=link,
        compiler=file_identity(Path(link[0]).resolve()),
        gpu_execution=False, default_replacement=False)
    try:
        run(link, BUILD, output / 'unwrapped-link.log')
        unwrapped_sha = sha(output / 'unwrapped')
        manifest['unwrapped_sha256'] = unwrapped_sha
        if unwrapped_sha != DEFAULT_SHA:
            raise RuntimeError('authentic objects did not reproduce accepted default')
        candidate = output / 'candidate'
        candidate.mkdir()
        frozen = candidate / 'source'
        frozen.mkdir()
        for path in source_paths + [plan_json, plan_header]:
            shutil.copy2(path, frozen / path.name)
            if sha(frozen / path.name) != originals[str(path)]['sha256']:
                raise RuntimeError('input changed while copying: ' + str(path))
        shutil.copy2(cache_file, candidate / 'CMakeCache.txt')
        shutil.copy2(link_file, candidate / 'original-link.txt')
        shutil.copy2(dependency_file, candidate / 'original-link.d')
        compile_command = [link[0], '-std=c++23', '-O2', '-Wall', '-Wextra',
                           '-Werror', '-I' + str(CUDA / 'include'),
                           '-I' + str(frozen), '-c',
                           str(frozen / 'memory_trace.cpp'), '-o',
                           str(candidate / 'trace.o')]
        manifest['compile'] = compile_command
        run(compile_command, BUILD, candidate / 'compile.log')
        wrapped_link = link.copy()
        wrapped_link[wrapped_link.index('-o') + 1] = str(candidate / 'q4t')
        wrapped_link.extend([str(candidate / 'trace.o'),
                             *[f'-Wl,--wrap={api}' for api in APIS]])
        manifest['candidate_link'] = wrapped_link
        run(wrapped_link, BUILD, candidate / 'link.log')
        manifest['candidate_sha256'] = sha(candidate / 'q4t')
        manifest['objects'] = dict(trace=file_identity(candidate / 'trace.o'),
                                   original_link_inputs=link_inputs)
        manifest['candidate'] = file_identity(candidate / 'q4t')
        manifest['real_build_cache'] = file_identity(candidate / 'CMakeCache.txt')
        contract_command = [link[0], '-std=c++23', '-O2', '-Wall', '-Wextra',
                            '-Werror', '-I' + str(CUDA / 'include'),
                            '-I' + str(frozen), str(frozen / 'gpu_contract.cpp'),
                            '-L' + str(CUDA / 'lib'),
                            '-Wl,-rpath,' + str(CUDA / 'lib'), '-lcudart',
                            '-o', str(candidate / 'gpu_contract')]
        manifest['gpu_contract_compile'] = contract_command
        run(contract_command, BUILD, candidate / 'gpu-contract-compile.log')
        manifest['gpu_contract_sha256'] = sha(candidate / 'gpu_contract')
        manifest['gpu_contract'] = file_identity(candidate / 'gpu_contract')
        for path, identity in originals.items():
            if sha(Path(path)) != identity['sha256']:
                raise RuntimeError('input changed during build: ' + path)
        for path, identity in link_inputs.items():
            if sha(Path(path)) != identity['sha256']:
                raise RuntimeError('link input changed during build: ' + path)
        if sha(default) != DEFAULT_SHA:
            raise RuntimeError('default binary changed during host build')
        manifest.update(status='COMPLETED', finished=time.time(),
                        default_unchanged=True)
        save(output / 'build.json', manifest)
        print(json.dumps(dict(output=str(output),
                              candidate_sha256=manifest['candidate_sha256'],
                              unwrapped_sha256=unwrapped_sha)))
        return 0
    except BaseException as error:
        manifest.update(status='FAILED', error=repr(error), finished=time.time(),
                        default_unchanged=sha(default) == DEFAULT_SHA)
        save(output / 'build-failure.json', manifest)
        raise


if __name__ == '__main__':
    raise SystemExit(main())

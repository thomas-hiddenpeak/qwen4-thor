"""Compile CPU contracts against the sealed, actual StartupPlan archives."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import struct
import subprocess

ROOT = Path(__file__).resolve().parents[3]
BASE = ROOT / '.q4t-work/startup-plan-20261020'


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def identity(path):
    stat = path.stat()
    return dict(path=str(path), sha256=sha(path), bytes=stat.st_size,
                mtime_ns=stat.st_mtime_ns)


def save(path, value):
    with path.open('x') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def fixture(output):
    directory = output / 'fixture'
    directory.mkdir()
    tensors = {
        'valid': ('BF16', [2, 3], [0, 12]),
        'scalar': ('F32', [], [12, 16]),
        'wrong_shape': ('BF16', [3, 2], [16, 28]),
        'wrong_dtype': ('F16', [2, 3], [28, 40]),
        'short_span': ('BF16', [2, 3], [40, 51]),
        'long_span': ('BF16', [2, 3], [52, 65]),
        'reversed_span': ('BF16', [2, 3], [80, 68]),
        'uncached_a': ('BF16', [1], [80, 82]),
    }
    weight_map = {name: 'a.safetensors' for name in tensors}
    for filename, values in (
            ('a.safetensors', tensors),
            ('b.safetensors', {'other_shard': ('BF16', [1], [0, 2])})):
        data = {name: dict(dtype=dtype, shape=shape, data_offsets=offsets)
                for name, (dtype, shape, offsets) in values.items()}
        header = json.dumps(data, separators=(',', ':')).encode()
        with (directory / filename).open('xb') as stream:
            stream.write(struct.pack('<Q', len(header)))
            stream.write(header)
            stream.write(bytes(128))
    weight_map['other_shard'] = 'b.safetensors'
    save(directory / 'model.safetensors.index.json',
         dict(metadata=dict(total_size=256), weight_map=weight_map))
    return directory


def run(command, output, label, *, cwd=ROOT, environment=None, timeout=60):
    save(output / (label + '-command.json'), command)
    result = subprocess.run(command, cwd=cwd, env=environment,
                            capture_output=True, text=True, timeout=timeout)
    (output / (label + '.stdout')).write_text(result.stdout)
    (output / (label + '.stderr')).write_text(result.stderr)
    save(output / (label + '-exit.json'), dict(returncode=result.returncode))
    require(result.returncode == 0, label + ': ' + result.stderr)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-manifest', type=Path, default=BASE / 'build.json')
    parser.add_argument('--output', type=Path, default=BASE / 'host-core-v1')
    args = parser.parse_args()
    manifest_path = args.build_manifest.resolve()
    manifest = json.loads(manifest_path.read_text())
    require(manifest['status'] == 'COMPLETED', 'build is not complete')
    build = Path(manifest['candidate']['path']).parent
    output = args.output.resolve()
    require(output.is_relative_to(ROOT / '.q4t-work'),
            'output must be within .q4t-work')
    require(not output.is_relative_to(build), 'do not write into sealed build')
    output.mkdir(parents=True, exist_ok=False)
    archives = {}
    link_path = build / 'CMakeFiles/q4t.dir/link.txt'
    original = shlex.split(link_path.read_text())
    # Keep the candidate's real libraries, but never execute its main or
    # reservation wrapper and never overwrite its linker dependency file.
    filtered = []
    removed = []
    cursor = 1
    while cursor < len(original):
        token = original[cursor]
        cursor += 1
        if token == '-o':
            cursor += 1
        elif token.startswith(('-Wl,--wrap=', '-Wl,--dependency-file=')):
            removed.append(token)
        elif token.endswith('.o'):
            require(token.endswith(('src/main.cpp.o',
                                    'startup_plan/reservation.cpp.o')),
                    'unexpected candidate object: ' + token)
            removed.append(token)
        elif token.endswith('.a') or token.endswith('.so'):
            path = (build / token).resolve()
            record = manifest['link_inputs'].get(str(path))
            require(record is not None and sha(path) == record['sha256'],
                    'unsealed or changed link input: ' + str(path))
            filtered.append(str(path))
            if path.parent == build and path.suffix == '.a':
                archives[str(path)] = identity(path)
        else:
            filtered.append(token)
    core = build / 'libq4t_startup_plan.a'
    require(str(core) in archives, 'actual StartupPlan archive absent')
    require(core.stat().st_mtime_ns <= manifest_path.stat().st_mtime_ns,
            'core archive is newer than sealed build manifest')
    for name in ('src/runtime/startup_plan.cpp',
                 'include/q4t/runtime/startup_plan.h'):
        path = ROOT / name
        require(sha(path) == manifest['source_and_plan_inputs'][str(path)]['sha256'],
                'core source differs from sealed build: ' + name)
    nm = run(['nm', '-u', *archives], output, 'undefined-symbols')
    gpu_symbols = sorted(set(re.findall(
        r'\bU\s+((?:cuda|cu|cublasLt)[A-Z][A-Za-z0-9_]*)\s*$',
        nm.stdout, re.MULTILINE)))
    require('cudaMalloc' in gpu_symbols and 'cudaMemcpyAsync' in gpu_symbols,
            'GPU operation guard symbol discovery incomplete')
    forbidden = gpu_symbols + ['pread', 'preadv']
    guard = output / 'forbidden_calls.cpp'
    guard.write_text(
        '#include <cstdio>\n#include <cstdlib>\n' + ''.join(
            'extern "C" [[noreturn]] void __wrap_' + name + '() {\n'
            '  std::fprintf(stderr, "FORBIDDEN_CALL: ' + name + '\\n");\n'
            '  std::_Exit(126);\n}\n' for name in forbidden))
    source = Path(__file__).with_suffix('.cpp')
    compiler = original[0]
    flags = ['-std=c++23', '-Wall', '-Wextra', '-Werror',
             '-I' + str(ROOT / 'include'), '-I/usr/local/cuda/include']
    for label, path in (('core', source), ('guard', guard)):
        run([compiler, *flags, '-c', str(path), '-o', str(output / (label + '.o'))],
            output, 'compile-' + label)
    binary = output / 'test_core'
    command = [compiler, str(output / 'core.o'), str(output / 'guard.o'),
               *filtered, '-Wl,--wrap=posix_fadvise',
               *['-Wl,--wrap=' + name for name in forbidden],
               '-o', str(binary)]
    run(command, output, 'link-core', cwd=build)
    data = fixture(output)
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith('Q4T_') and key != 'LD_PRELOAD'}
    result = run([str(binary), str(data)], output, 'core-contract',
                 environment=environment, timeout=15)
    rows = result.stdout.splitlines()
    summary = json.loads(rows[-1])
    require(summary['status'] == 'PASS' and summary['count'] ==
            sum(row.startswith('PASS: ') for row in rows),
            'contract summary mismatch')
    require('FORBIDDEN_CALL' not in result.stdout + result.stderr,
            'GPU operation or payload read attempted')
    for path, before in archives.items():
        require(sha(Path(path)) == before['sha256'], 'archive changed during test')
    save(output / 'result.json', dict(
        **summary, build_manifest=identity(manifest_path),
        candidate_sha256=manifest['candidate_sha256'], archives=archives,
        sources=[identity(source), identity(Path(__file__).resolve())],
        linker_source=identity(link_path), removed_candidate_arguments=removed,
        forbidden_calls=forbidden, gpu_execution=False,
        guard_scope='direct GPU runtime/driver/BLAS operations from project '
                    'archives; host CUDA module registration is permitted'))
    save(output / 'sealed-files.json', {
        str(path.relative_to(output)): identity(path)
        for path in sorted(output.rglob('*')) if path.is_file()})
    print(json.dumps(summary))


if __name__ == '__main__':
    main()

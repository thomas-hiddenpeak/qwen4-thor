"""Build a frozen product candidate and host gate; never run CUDA or deploy.

The experimental startup switch is explicitly OFF. Build directories and
source snapshots are new per attempt, and failed attempts are retained.
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
BASE = ROOT / '.q4t-work/startup-delivery-20261021'
DEFAULT_SHA = '19ea4e57bf3a0e4f568a3e59c31ccbb4e71ad535db997682d8160d4be2f9c469'
SOURCE_ROOTS = ('CMakeLists.txt', 'src', 'include', 'cmake', 'third_party',
                'tests', 'tools')
WARNING = re.compile(r'\bwarning(?:\s*#[\w-]+)?\s*:|'
                     r'\bCMake(?: Deprecation)? Warning\b',
                     re.IGNORECASE)


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def identity(path):
    path = Path(path).resolve(strict=True)
    return dict(path=str(path), sha256=sha(path), bytes=path.stat().st_size)


def save(path, value):
    with Path(path).open('x') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def check_identity(record):
    path = Path(record['path']).resolve(strict=True)
    require(path.is_file() and path.stat().st_size == record['bytes'] and
            sha(path) == record['sha256'], 'identity differs: ' + str(path))
    return path


def source_names():
    raw = subprocess.check_output(
        ['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard',
         '--', *SOURCE_ROOTS], cwd=ROOT)
    return sorted({name.decode() for name in raw.split(b'\0') if name and
                   (ROOT / name.decode()).is_file()})


def default_pair():
    binary, sidecar = ROOT / 'build/q4t', ROOT / 'build/q4t.release.json'
    metadata = json.loads(sidecar.read_text())
    require(sha(binary) == metadata['binary_sha256'] == DEFAULT_SHA,
            'default binary/release pair differs from accepted 19ea')
    cache = Path(metadata['build_cache'])
    require(sha(cache) == metadata['build_cache_sha256'],
            'default release build cache differs')
    return dict(binary=identity(binary), sidecar=identity(sidecar),
                build_cache=identity(cache))


def freeze_sources(output):
    names = source_names()
    require('CMakeLists.txt' in names and names, 'missing build sources')
    originals, frozen = {}, {}
    source = output / 'source'
    source.mkdir()
    for name in names:
        path = ROOT / name
        require(path.resolve().is_relative_to(ROOT),
                'source escapes the workspace: ' + name)
        originals[name] = identity(path)
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        frozen[name] = identity(target)
        require(frozen[name]['sha256'] == originals[name]['sha256'],
                'source changed during snapshot: ' + name)
    require(source_names() == names, 'source file set changed during snapshot')
    for record in originals.values():
        check_identity(record)
    save(output / 'source-inputs.json', dict(originals=originals, frozen=frozen))
    return source, originals, frozen


def dependency_paths(path, directory):
    """Read the first Make dependency rule, including escaped whitespace."""
    rule = Path(path).read_text().replace('\\\n', ' ').splitlines()[0]
    require(':' in rule, 'missing dependency rule: ' + str(path))
    result = set()
    for token in shlex.split(rule.split(':', 1)[1]):
        candidate = Path(token)
        candidate = candidate if candidate.is_absolute() else directory / candidate
        result.add(candidate.resolve(strict=True))
    require(result, 'empty dependency rule: ' + str(path))
    return result


def cache_values(path):
    result = {}
    for line in Path(path).read_text().splitlines():
        if line and not line.startswith(('#', '//')) and ':' in line and '=' in line:
            key, rest = line.split(':', 1)
            _, value = rest.split('=', 1)
            result[key] = value
    return result


def verify_product_configuration(build, source):
    cache = cache_values(build / 'CMakeCache.txt')
    require(cache.get('Q4T_STARTUP_RESOURCE_PLAN') == 'OFF',
            'research startup adapter must remain OFF')
    require(cache.get('Q4T_BUILD_TESTS') == 'ON' and
            cache.get('CMAKE_BUILD_TYPE') == 'Release' and
            cache.get('Q4T_CUDA_ARCHITECTURES') == '110a',
            'product build configuration differs')
    require(Path(cache['CMAKE_HOME_DIRECTORY']).resolve() == source.resolve(),
            'build did not use the frozen source snapshot')
    commands = json.loads((build / 'compile_commands.json').read_text())
    server = [row for row in commands if Path(row['file']).resolve() ==
              source / 'src/server/chat_server.cpp']
    require(len(server) == 1, 'expected one compiled server implementation')
    arguments = server[0].get('arguments')
    if arguments is None:
        arguments = shlex.split(server[0].get('command', ''))
    require('-DQ4T_STARTUP_RESERVATION=1' in arguments,
            'formal startup reservation is not compiled into the server')
    return dict(research_adapter='OFF', product_macro='Q4T_STARTUP_RESERVATION=1',
                build_type='Release', architecture='110a', host_gate_built=True)


def verify(manifest_path, check_workspace=True, check_default=True):
    """Read-only identity audit; no candidate or CUDA program is executed."""
    manifest_path = Path(manifest_path).resolve(strict=True)
    require(not (manifest_path.parent / 'build-failure.json').exists(),
            'build attempt recorded a failure')
    manifest = json.loads(manifest_path.read_text())
    require(manifest['status'] == 'COMPLETED' and
            manifest['gpu_execution'] is False and
            manifest['default_replacement'] is False,
            'candidate build is not completed')
    for key in ('candidate', 'host_test_binary', 'real_build_cache',
                'compile_commands', 'source_manifest'):
        check_identity(manifest[key])
    require(manifest['candidate_sha256'] == manifest['candidate']['sha256'],
            'candidate hash alias differs')
    for key in ('source_snapshot_inputs', 'compile_inputs', 'link_inputs',
                'dependency_files', 'toolchain'):
        require(manifest[key], 'missing identity group: ' + key)
        for record in manifest[key].values():
            check_identity(record)
    if check_workspace:
        for record in manifest['source_inputs'].values():
            check_identity(record)
    if check_default:
        require(default_pair() == manifest['default_pair'],
                'default pair changed after candidate build')
    for record in manifest['logs']:
        path = check_identity(record)
        require(not WARNING.search(path.read_text(errors='replace')),
                'warning in accepted build log: ' + str(path))
    configuration = verify_product_configuration(
        Path(manifest['candidate']['path']).parent,
        Path(manifest['source_snapshot']))
    require(configuration == manifest['configuration'],
            'configuration report differs')
    require(not any('tools/verify/startup_plan/reservation.cpp' in path
                    for path in manifest['compile_inputs']),
            'research CUDA wrapper was compiled into this build')
    return dict(status='PASS', candidate_sha256=manifest['candidate_sha256'],
                default_unchanged=check_default, workspace_checked=check_workspace,
                gpu_execution=False, runtime_accepted=False)


def build_candidate(output, jobs):
    output = output.resolve()
    require(output.is_relative_to(ROOT / '.q4t-work'),
            'output must be a new .q4t-work directory')
    require(1 <= jobs <= 8, 'build jobs must be in [1,8]')
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    default = default_pair()
    save(output / 'attempt.json', dict(status='BUILDING', started_mono=started,
        default_pair=default, gpu_execution=False, default_replacement=False,
        source_commit=subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()))
    logs, executed = [], []
    try:
        source, originals, frozen = freeze_sources(output)
        build = output / 'build'
        cmake = Path(shutil.which('cmake')).resolve(strict=True)
        cxx = Path(shutil.which('g++-14')).resolve(strict=True)
        nvcc = Path('/usr/local/cuda/bin/nvcc').resolve(strict=True)
        toolchain = {str(p): identity(p) for p in (cmake, cxx, nvcc)}
        commands = [
            [str(cmake), '-S', str(source), '-B', str(build),
             '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_CXX_COMPILER=' + str(cxx),
             '-DCMAKE_CUDA_HOST_COMPILER=' + str(cxx),
             '-DQ4T_CUDA_ARCHITECTURES=110a', '-DQ4T_BUILD_TESTS=ON',
             '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON',
             '-DQ4T_STARTUP_RESOURCE_PLAN=OFF'],
            [str(cmake), '--build', str(build), '--parallel', str(jobs),
             '--target', 'q4t', 'q4t_host_tests'],
        ]
        save(output / 'commands.json', commands)
        for index, command in enumerate(commands):
            log = output / f'build-{index}.log'
            logs.append(log)
            with log.open('x') as stream:
                result = subprocess.run(command, cwd=ROOT, stdout=stream,
                                        stderr=subprocess.STDOUT, check=False)
            executed.append(dict(command=command, returncode=result.returncode,
                                 log=identity(log)))
            require(result.returncode == 0, 'build command failed: ' + str(log))
            require(not WARNING.search(log.read_text(errors='replace')),
                    'compiler/configuration warning: ' + str(log))
        configuration = verify_product_configuration(build, source)
        dependency_files = sorted((build / 'CMakeFiles').rglob('*.o.d'))
        require(dependency_files, 'compiler dependency files are missing')
        compile_paths = set()
        for path in dependency_files:
            compile_paths.update(dependency_paths(path, build))
        require(not any('tools/verify/startup_plan/reservation.cpp' in str(p)
                        for p in compile_paths), 'research wrapper was compiled')
        link_paths = set()
        for target in ('q4t', 'q4t_host_tests'):
            path = build / f'CMakeFiles/{target}.dir/link.d'
            dependency_files.append(path)
            link_paths.update(dependency_paths(path, build))
        # The candidate compiles the snapshot, never the working tree. Only
        # actual compiler inputs and CMake definitions bind the live sources;
        # uncompiled verification tools may still be developed in parallel.
        live_names = {str(path.relative_to(source)) for path in compile_paths
                      if path.is_relative_to(source)}
        live_names.update(name for name in originals if
                          name == 'CMakeLists.txt' or name.startswith('cmake/'))
        used_originals = {name: originals[name] for name in sorted(live_names)}
        for record in [*used_originals.values(), *frozen.values(),
                       *toolchain.values()]:
            check_identity(record)
        require(default_pair() == default, 'default pair changed during build')
        candidate = identity(build / 'q4t')
        manifest = dict(status='COMPLETED', candidate=candidate,
            candidate_sha256=candidate['sha256'],
            host_test_binary=identity(build / 'q4t_host_tests'),
            default_pair=default, default_sha256=DEFAULT_SHA,
            default_replacement=False, gpu_execution=False,
            runtime_accepted=False, host_tests_executed=False,
            source_snapshot=str(source), source_inputs=used_originals,
            source_snapshot_inputs=frozen,
            source_manifest=identity(output / 'source-inputs.json'),
            compile_inputs={str(p): identity(p) for p in sorted(compile_paths)},
            link_inputs={str(p): identity(p) for p in sorted(link_paths)},
            dependency_files={str(p): identity(p) for p in dependency_files},
            real_build_cache=identity(build / 'CMakeCache.txt'),
            compile_commands=identity(build / 'compile_commands.json'),
            toolchain=toolchain, configuration=configuration,
            commands=executed, logs=[identity(p) for p in logs],
            elapsed_seconds=time.monotonic() - started)
        save(output / 'build.json', manifest)
        checked = verify(output / 'build.json')
        save(output / 'build-audit.json', checked)
        print(json.dumps(checked))
    except BaseException as error:
        unchanged = False
        try:
            unchanged = default_pair() == default
        except (OSError, ValueError, RuntimeError):
            pass
        save(output / 'build-failure.json', dict(status='FAILED', error=repr(error),
            commands=executed, logs=[str(p) for p in logs],
            default_unchanged=unchanged, gpu_execution=False,
            elapsed_seconds=time.monotonic() - started))
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=BASE / 'candidate')
    parser.add_argument('--jobs', type=int, default=6)
    parser.add_argument('--verify', type=Path,
                        help='read-only audit of an existing build.json')
    args = parser.parse_args()
    if args.verify:
        print(json.dumps(verify(args.verify)))
    else:
        build_candidate(args.output, args.jobs)


if __name__ == '__main__':
    main()

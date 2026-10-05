"""One clean committed export/build; no tests or model execution."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tarfile
import time

R = Path(__file__).resolve().parent
W = R / 'source'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    with path.open('x') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')


assert not subprocess.check_output(['git', 'status', '--porcelain'], cwd=W)
commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=W,
                                 text=True).strip()
source = R / 'candidate-source'
build = R / 'candidate-build'
archive = R / 'candidate-source.tar'
assert not source.exists() and not build.exists() and not archive.exists()
subprocess.run(['git', 'archive', '--format=tar', '-o', str(archive), commit],
               cwd=W, check=True)
source.mkdir()
with tarfile.open(archive) as stream:
    stream.extractall(source, filter='data')
save(R / 'candidate-source-identity.json', {
    'commit': commit, 'source': str(source), 'clean_export': True,
    'excluded_original_dirty_paths': 7, 'created_t': time.time(),
    'archive_sha256': sha(archive)})
commands = [
    ('configure', ['cmake', '-S', str(source), '-B', str(build),
                   '-DCMAKE_BUILD_TYPE=Release',
                   '-DCMAKE_CXX_COMPILER=/usr/bin/g++-14',
                   '-DCMAKE_CUDA_HOST_COMPILER=/usr/bin/g++-14',
                   '-DQ4T_CUDA_ARCHITECTURES=110a',
                   '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON']),
    ('build', ['cmake', '--build', str(build), '--target', 'q4t',
               '--parallel', '8'])]
records = []
failure = None
try:
    for name, command in commands:
        record = {'name': name, 'command': command, 'started_t': time.time()}
        with (R / (name + '.log')).open('x') as log:
            proc = subprocess.run(command, cwd=W, stdout=log,
                                  stderr=subprocess.STDOUT)
        record.update(ended_t=time.time(), returncode=proc.returncode)
        records.append(record)
        assert proc.returncode == 0, name + ' failed'
    warnings = sum(len(re.findall(r'warning\s*:|warning\s*#|cmake\s+warning',
                                  (R / (name + '.log')).read_text(), re.I))
                   for name, _ in commands)
    assert warnings == 0, 'build has warnings'
    save(R / 'build-identity.json', {
        'source_commit': commit, 'source': str(source),
        'binary': str(build / 'q4t'), 'binary_sha256': sha(build / 'q4t'),
        'cmake_cache_sha256': sha(build / 'CMakeCache.txt'),
        'compile_commands_sha256': sha(build / 'compile_commands.json'),
        'warnings': warnings, 'build_rc': 0, 'records': records,
        'runtime_tests_before_http': False, 'ended_t': time.time()})
except BaseException as error:
    failure = type(error).__name__ + ': ' + str(error)
    raise
finally:
    save(R / 'build-attempt.json', {
        'source_commit': commit, 'records': records, 'failure': failure,
        'ended_t': time.time(), 'tests_or_model_executed': False})
print('Clean Thor build complete, zero warnings; tests not run.', flush=True)

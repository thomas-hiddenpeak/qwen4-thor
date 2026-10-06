"""Export one clean commit and build q4t on Thor; no tests are run."""
import hashlib
import re
import subprocess
import tarfile
import time

from recycle_common import R, W, sha, save, run

assert not subprocess.check_output(['git', 'status', '--porcelain'], cwd=W).strip()
commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=W,
                                 text=True).strip()
archive = R / 'candidate-source.tar'
source = R / 'candidate-source'
build = R / 'candidate-build'
assert not archive.exists() and not source.exists() and not build.exists()
subprocess.run(['git', 'archive', '--format=tar', '--output', str(archive),
                commit], cwd=W, check=True)
source.mkdir()
with tarfile.open(archive) as contents:
    contents.extractall(source, filter='data')
sources = {str(p): sha(p) for p in sorted(source.rglob('*')) if p.is_file()}
save(R / 'candidate-source-identity.json', dict(commit=commit,
    source=str(source), archive_sha256=sha(archive), source_sha256=sources,
    exported_files=len(sources), recorded_t=time.time()))
report = dict(source_commit=commit, source=str(source), build=str(build),
    source_sha256=sources, warnings=None, build_rc=None, binary_sha256=None,
    started_t=time.time(), failure=None, tests_executed=0)
try:
    receipt = run(['cmake', '-S', str(source), '-B', str(build),
        '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_CXX_COMPILER=/usr/bin/g++-14',
        '-DCMAKE_CUDA_HOST_COMPILER=/usr/bin/g++-14',
        '-DQ4T_CUDA_ARCHITECTURES=110a', '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON'],
        R, 'configure-01', cwd=W, timeout=600)
    assert receipt['returncode'] == 0 and receipt['failure'] is None
    receipt = run(['cmake', '--build', str(build), '--target', 'q4t',
        '--parallel', '8'], R, 'build-01', cwd=W, timeout=1800)
    report['build_rc'] = receipt['returncode']
    assert receipt['returncode'] == 0 and receipt['failure'] is None
    text = (R / 'configure-01.log').read_text() + (R / 'build-01.log').read_text()
    warnings = re.findall(r'warning\s*:|warning\s*#|cmake\s+warning', text, re.I)
    report['warnings'] = len(warnings)
    assert not warnings
    report['binary_sha256'] = sha(build / 'q4t')
    for path, digest in sources.items():
        assert sha(path) == digest, path
    for name in ['CMakeCache.txt', 'compile_commands.json']:
        report['source_sha256'][str(build / name)] = sha(build / name)
    report['cmake_cache_sha256'] = sha(build / 'CMakeCache.txt')
    report['compile_commands_sha256'] = sha(build / 'compile_commands.json')
    report['source_sha256'][str(archive)] = sha(archive)
    report['source_sha256'][str(R / 'candidate-source-identity.json')] = sha(
        R / 'candidate-source-identity.json')
except BaseException as error:
    report['failure'] = type(error).__name__ + ': ' + str(error)
    raise
finally:
    report['ended_t'] = time.time()
    save(R / 'build-identity.json', report)
    print({k: report[k] for k in ('source_commit', 'build_rc', 'warnings',
                                 'binary_sha256', 'failure')}, flush=True)

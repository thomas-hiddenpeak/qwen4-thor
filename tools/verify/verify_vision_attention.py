"""Run the actual vision attention against exact cases after all HTTP gates."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require_http(run):
    gate = json.loads((run / 'acceptance.json').read_text())
    assert gate['quality_passed'] and gate['performance_accepted']
    visual = json.loads((run / 'vision-http/review.json').read_text())
    assert visual['http_determinism_passed'] and visual['performance_accepted']
    binary_sha = sha(ROOT / 'build/q4t')
    for mode, count in [('quality', 11), ('performance', 5)]:
        terminal = json.loads((run / mode / 'exit.json').read_text())
        assert terminal['completed'] == count and terminal['server'] == 0
        assert terminal['failure'] is None and terminal['http_output_checks_passed']
        assert binary_sha == (run / mode / 'binary.sha256').read_text().strip()
    for group in ['parent-before', 'candidate', 'candidate-restart', 'parent-after']:
        assert json.loads((run / 'vision-http' / group / 'exit.json').read_text())['server'] == 0
        if group.startswith('candidate'):
            assert binary_sha == (run / 'vision-http' / group / 'binary.sha256').read_text().strip()
    for snapshot in (run / 'source').rglob('*'):
        if snapshot.is_file():
            assert snapshot.read_bytes() == (ROOT / snapshot.relative_to(run / 'source')).read_bytes()
    libraries = json.loads((run / 'libraries.json').read_text())
    for name, expected in libraries.items():
        assert sha(ROOT / name) == expected
    return binary_sha, libraries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--accepted-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    run, out = args.accepted_root.resolve(), args.output.resolve()
    assert any(out.is_relative_to(ROOT / p) for p in ['build', '.q4t-work'])
    binary_sha, libraries = require_http(run)
    out.mkdir(parents=True, exist_ok=False)
    source = Path(__file__).with_suffix('.cu.in')
    shutil.copy2(__file__, out)
    shutil.copy2(source, out / 'comparison.cu')
    shutil.copy2(ROOT / 'src/vision/vision.cu', out / 'vision-source.cu')
    cache = (ROOT / 'build/CMakeCache.txt').read_text()
    compiler = re.search(r'^CMAKE_CUDA_COMPILER:[^=]+=([^\n]+)', cache, re.M)[1]
    host = re.search(r'^CMAKE_CUDA_HOST_COMPILER:[^=]+=([^\n]+)', cache, re.M)[1]
    command = [compiler, '-std=c++23', '--expt-relaxed-constexpr', '-O3',
        '-arch=sm_110a', '-ccbin', host, '-Xcompiler=-Wall,-Wextra',
        '-I' + str(ROOT), '-I' + str(ROOT / 'include'), str(out / 'comparison.cu'),
        '-Xlinker=--start-group']
    command += [str(ROOT / f'build/libq4t_{name}.a') for name in
                ['model', 'ple', 'quant', 'text', 'io', 'runtime']]
    command += ['-Xlinker=--end-group', '-lcublasLt', '-luring', '-licui18n',
                '-licuuc', '-licudata', '-ldl', '-lpthread', '-lrt',
                '-o', str(out / 'comparison')]
    (out / 'manifest.json').write_text(json.dumps({'binary_sha256':binary_sha,
        'source_sha256':sha(ROOT / 'src/vision/vision.cu'),
        'libraries':libraries,'command':command}, indent=2))
    with (out / 'build.log').open('w') as log:
        subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
    assert 'warning' not in (out / 'build.log').read_text().lower()
    with (out / 'result.log').open('w') as log:
        result = subprocess.run([str(out / 'comparison')], stdout=log, stderr=subprocess.STDOUT)
    (out / 'exit.json').write_text(json.dumps({'numerical':result.returncode,
        'limits':'Exact uniform-attention cases, not a full vision-tower reference.'}))
    result.check_returncode()


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Build/run the real support selector with CPU-only, controllable FP8 state.

Only CheckStartupPlanSupport is retained from the actual startup_plan.cpp.
No CUDA library, metadata loader, payload reader or budget implementation is
linked. Configuration support is not an assertion that a budget will fit.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[3]
BASE = ROOT / '.q4t-work/startup-delivery-20261021'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, data):
    with path.open('x') as stream:
        json.dump(data, stream, indent=2)
        stream.write('\n')


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def run(command, output, label):
    save(output / (label + '-command.json'), command)
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith('Q4T_') and key != 'LD_PRELOAD'}
    result = subprocess.run(command, cwd=ROOT, env=environment,
                            capture_output=True, text=True, timeout=60)
    (output / (label + '.stdout')).write_text(result.stdout)
    (output / (label + '.stderr')).write_text(result.stderr)
    save(output / (label + '-exit.json'), {'returncode': result.returncode})
    require(result.returncode == 0, label + ': ' + result.stderr)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=BASE / 'host-support-v1')
    parser.add_argument('--compiler', default='g++-14')
    args = parser.parse_args()
    output = args.output.resolve()
    require(output.is_relative_to(BASE.resolve()), 'output must use new BASE')
    output.mkdir(parents=True, exist_ok=False)
    compiler = shutil.which(args.compiler)
    require(compiler is not None, 'compiler unavailable')
    source = ROOT / 'src/runtime/startup_plan.cpp'
    contract = ROOT / 'tests/startup_plan_support_test.cpp'
    inputs = [source, contract, Path(__file__).resolve(),
              ROOT / 'include/q4t/runtime/startup_plan.h',
              ROOT / 'include/q4t/model/model.h',
              ROOT / 'include/q4t/model/gemv.h',
              ROOT / 'include/q4t/server/server_options.h',
              ROOT / 'include/q4t/status.h', ROOT / 'include/q4t/test.h']
    before = {str(path.relative_to(ROOT)): sha(path) for path in inputs}
    save(output / 'source-before.json', before)
    flags = ['-std=c++23', '-O2', '-Wall', '-Wextra', '-Werror',
             '-ffunction-sections', '-fdata-sections',
             '-I' + str(ROOT / 'include'), '-I/usr/local/cuda/include']
    for name, path in [('implementation', source), ('contract', contract)]:
        extra = ['-DQ4T_STARTUP_SUPPORT_HOST_ONLY=1'] if name == 'contract' else []
        run([compiler, *flags, *extra, '-c', str(path),
             '-o', str(output / (name + '.o'))], output, 'compile-' + name)
    binary = output / 'test_support'
    run([compiler, str(output / 'implementation.o'),
         str(output / 'contract.o'), '-Wl,--gc-sections', '-o', str(binary)],
        output, 'link')
    undefined = run(['nm', '-u', str(binary)], output, 'undefined-symbols').stdout
    forbidden = re.findall(
        r'\b(?:cuda[A-Z]\w*|cu[A-Z]\w*|cublas\w*|pread\w*|mmap\w*|'
        r'open(?:64)?|fopen(?:64)?|posix_fadvise\w*)\b', undefined)
    require(not forbidden, 'unexpected device or file I/O symbol: ' +
            ', '.join(forbidden))
    dynamic = run(['readelf', '-d', str(binary)], output, 'dynamic-links').stdout
    require(not re.search(r'NEEDED.*(?:cuda|nvidia|cublas)', dynamic, re.I),
            'GPU library linked into CPU support contract')
    result = run([str(binary)], output, 'contracts')
    rows = result.stdout.splitlines()
    passed = sum(line.startswith('[PASS] ') for line in rows)
    require(passed > 0 and not any(line.startswith(('[FAIL]', '[SKIP]'))
                                  for line in rows), 'contracts failed/skipped')
    require(rows[-1] == f'{passed} tests, {passed} passed, 0 failed, 0 skipped',
            'contract summary mismatch')
    require(all(sha(path) == before[str(path.relative_to(ROOT))]
                for path in inputs), 'source changed during host contract')
    save(output / 'result.json', {
        'status': 'PASS', 'count': passed, 'gpu_execution': False,
        'metadata_or_payload_io': False,
        'source_sha256': before, 'binary_sha256': sha(binary),
        'actual_implementation': 'src/runtime/startup_plan.cpp',
        'fp8_selection': 'controllable CPU stub plus actual environment checks',
        'scope': 'support selection, not full metadata plan or model execution'})
    save(output / 'sealed-files.json', {
        str(path.relative_to(output)): {'sha256': sha(path),
                                      'bytes': path.stat().st_size}
        for path in sorted(output.rglob('*')) if path.is_file()})
    print(json.dumps({'status': 'PASS', 'count': passed, 'output': str(output)}))


if __name__ == '__main__':
    main()

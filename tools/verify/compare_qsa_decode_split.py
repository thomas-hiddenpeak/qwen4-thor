"""Compare QSA output partitions only after complete HTTP E2E acceptance.

This is a numerical check, not a benchmark. The same finite BF16 inputs exercise
partial tiles, buffer swaps, nonidentity pages and pooled sequence IDs. Kernel
bodies are copied verbatim from Git/current source, not separately maintained.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
SOURCE = 'src/model/full_attention.cu'
HEADER = 'include/q4t/model/full_attention.h'


def save(path, obj):
    path.write_text(json.dumps(obj, indent=2) + '\n')


def kernel_source(source, header, namespace):
    # The fixture is intentionally model-specific. Reject a changed page shape.
    if not re.search(r'constexpr int kKvPageSize\s*=\s*16\s*;', header):
        raise ValueError('fixture requires 16-position KV pages')
    begin = source.index('__device__ __forceinline__ f32 Bf16ToFloat(')
    end = source.index('// Deterministic block reduction', begin)
    converters = source[begin:end]
    begin = source.index('__device__ __forceinline__ void MmaBf16(')
    end = source.index('\n}  // namespace', begin)
    return (f'namespace {namespace} {{\nusing u16 = uint16_t;\n'
            'using f32 = float;\nconstexpr int kKvPageSize = 16;\n'
            + converters + source[begin:end] + '\n}\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--accepted-root', type=Path, required=True,
                        help='Accepted quality/performance run root')
    parser.add_argument('--baseline-ref', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    accepted = args.accepted_root.resolve()
    approval = json.loads((accepted / 'acceptance.json').read_text())
    if not approval.get('performance_accepted'):
        parser.error('requires explicit E2E performance acceptance')
    for mode, count in [('quality', 11), ('performance', 5)]:
        gate = json.loads((accepted / mode / 'exit.json').read_text())
        if (not gate['http_output_checks_passed'] or gate['server'] != 0
                or gate['completed'] != count):
            parser.error('requires completed quality and five-length HTTP E2E')
    binary_sha = hashlib.sha256((ROOT / 'build/q4t').read_bytes()).hexdigest()
    expected_sha = (accepted / 'performance/binary.sha256').read_text().strip()
    quality_sha = (accepted / 'quality/binary.sha256').read_text().strip()
    if binary_sha != expected_sha or binary_sha != quality_sha:
        parser.error('q4t binary differs from accepted HTTP run')
    out = args.output.resolve()
    if not any(out.is_relative_to(ROOT / p) for p in ['build', '.q4t-work']):
        parser.error('output must be under build/ or .q4t-work/')
    out.mkdir(parents=True, exist_ok=False)
    baseline_ref = subprocess.check_output(
        ['git', 'rev-parse', '--verify', args.baseline_ref + '^{commit}'],
        cwd=ROOT, text=True).strip()
    before = subprocess.check_output(
        ['git', 'show', f'{baseline_ref}:{SOURCE}'], cwd=ROOT, text=True)
    before_header = subprocess.check_output(
        ['git', 'show', f'{baseline_ref}:{HEADER}'], cwd=ROOT, text=True)
    after = (ROOT / 'src/model/qsa_decode.cu').read_text()
    if after != (accepted / 'qsa_decode.cu').read_text():
        parser.error('candidate differs from HTTP source snapshot')
    (out / 'baseline.cu.txt').write_text(before)
    (out / 'candidate.cu.txt').write_text(after)
    fixture = Path(__file__).with_name('qsa_decode_split.cu.in')
    harness = fixture.read_text()
    (out / fixture.name).write_text(harness)
    (out / Path(__file__).name).write_text(Path(__file__).read_text())
    source = ('#include <cuda_runtime.h>\n#include <cuda_bf16.h>\n'
              '#include <array>\n#include <cstdint>\n#include <cstdio>\n'
              '#include <cstring>\n#include <vector>\n'
              + kernel_source(before, before_header, 'baseline')
              + after
              + harness)
    (out / 'comparison.cu').write_text(source)
    cache = (ROOT / 'build/CMakeCache.txt').read_text()
    compiler = re.search(
        r'^CMAKE_CUDA_COMPILER:[^=]+=([^\n]+)', cache, re.M)[1]
    host = re.search(
        r'^CMAKE_CUDA_HOST_COMPILER:[^=]+=([^\n]+)', cache, re.M)[1]
    command = [compiler, '-std=c++23', '--expt-relaxed-constexpr', '-O3',
               '-arch=sm_110a', '-ccbin', host, '-Xcompiler=-Wall,-Wextra',
               '-I' + str(ROOT / 'include'), str(out / 'comparison.cu'), '-o', str(out / 'comparison')]
    save(out / 'manifest.json', {
        'baseline_ref': baseline_ref, 'binary_sha256': binary_sha,
        'baseline_source_sha256': hashlib.sha256(before.encode()).hexdigest(),
        'candidate_source_sha256':
            hashlib.sha256(after.encode()).hexdigest(),
        'compile_command': command, 'accepted_root': str(accepted)})
    with (out / 'build.log').open('w') as log:
        subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                       check=True)
    if 'warning:' in (out / 'build.log').read_text():
        raise RuntimeError('numerical check build emitted warnings')
    with (out / 'result.log').open('w') as log:
        result = subprocess.run([str(out / 'comparison')], stdout=log,
                                stderr=subprocess.STDOUT)
    save(out / 'exit.json', {'numerical': result.returncode})
    print((out / 'result.log').read_text(), end='')
    result.check_returncode()


if __name__ == '__main__':
    main()

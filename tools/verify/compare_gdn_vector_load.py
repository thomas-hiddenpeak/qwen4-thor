"""Compare GDN outputs and persistent states after complete HTTP acceptance."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def extract(source):
    helpers = source[source.index('__device__ __forceinline__ float Bf16ToFloat'):
                     source.index('__device__ __forceinline__ float Silu')]
    kernel = source[source.index('template <int ROWS>\n__global__ void GatedDeltaNetRegKernel'):
                    source.index('// ---- Chunked tensor-core Gated DeltaNet')]
    return helpers + kernel


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--accepted-run', type=Path, required=True)
    parser.add_argument('--baseline-ref', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    run, out = args.accepted_run.resolve(), args.output.resolve()
    if not any(out.is_relative_to(ROOT / d) for d in ['build', '.q4t-work']):
        parser.error('output must be under build/ or .q4t-work/')
    if not json.loads((run / 'acceptance.json').read_text()).get('performance_accepted'):
        parser.error('complete HTTP performance acceptance required')
    digest = hashlib.sha256((ROOT / 'build/q4t').read_bytes()).hexdigest()
    for name, count in [('quality', 11), ('performance', 5)]:
        gate = json.loads((run / name / 'exit.json').read_text())
        if (not gate['http_output_checks_passed'] or gate['completed'] != count
                or gate['server'] != 0):
            parser.error(f'incomplete {name} HTTP gate')
        if (run / name / 'binary.sha256').read_text().strip() != digest:
            parser.error('binary differs from accepted binary')
    candidate = (ROOT / 'src/model/linear_attention.cu').read_text()
    if candidate != (run / 'linear_attention.cu').read_text():
        parser.error('candidate differs from accepted snapshot')
    baseline = subprocess.check_output(
        ['git', 'show', f'{args.baseline_ref}:src/model/linear_attention.cu'],
        cwd=ROOT, text=True)
    template = Path(__file__).with_suffix('.cu.in')
    source = template.read_text().replace('@ORIGINAL@', extract(baseline)).replace(
        '@CANDIDATE@', extract(candidate))
    out.mkdir(parents=True, exist_ok=False)
    (out / 'original.cu').write_text(baseline)
    (out / 'candidate.cu').write_text(candidate)
    (out / 'compare.cu').write_text(source)
    shutil.copy2(__file__, out / Path(__file__).name)
    shutil.copy2(template, out / template.name)
    command = ['/usr/local/cuda-13.3/bin/nvcc', '-std=c++23', '-O3',
               '-arch=sm_110a', '-ccbin=g++-14', '-Xcompiler=-Wall,-Wextra',
               str(out / 'compare.cu'), '-o', str(out / 'compare')]
    manifest = {'binary_sha256': digest, 'baseline_ref': args.baseline_ref,
                'baseline_commit': subprocess.check_output(
                    ['git', 'rev-parse', args.baseline_ref], cwd=ROOT, text=True).strip(),
                'candidate_sha256': hashlib.sha256(candidate.encode()).hexdigest(),
                'build_command': command, 'run_command': [str(out / 'compare')]}
    (out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    with (out / 'build.log').open('w') as log:
        subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
    if 'warning' in (out / 'build.log').read_text().lower():
        raise RuntimeError('build warning; inspect build.log')
    with (out / 'run.log').open('w') as log:
        subprocess.run(manifest['run_command'], stdout=log,
                       stderr=subprocess.STDOUT, check=True)
    result = json.loads((out / 'run.log').read_text())
    if (result['cases'] != 240 or result['state_values'] == 0
            or result['output_values'] == 0 or result['mismatches']
            or result['nonfinite']):
        raise RuntimeError('unexpected comparison results')
    (out / 'results.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()

"""Check fixed-shape MoE stages only after complete HTTP acceptance."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


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
            parser.error(f'incomplete {name} gate')
        if (run / name / 'binary.sha256').read_text().strip() != digest:
            parser.error('binary differs from HTTP binary')
    candidate = (ROOT / 'src/quant/moe_decode.cu').read_text()
    if candidate != (run / 'moe_decode.cu').read_text():
        parser.error('candidate differs from HTTP snapshot')
    baseline = subprocess.check_output(
        ['git', 'show', f'{args.baseline_ref}:src/quant/moe_gemm.cu'],
        cwd=ROOT, text=True)
    start = baseline.index('__device__ __forceinline__ float Bf16ToFloat')
    kernels = baseline[start:baseline.index('}  // namespace', start)]
    template = Path(__file__).with_suffix('.cu.in')
    generated = template.read_text().replace('@CANDIDATE@', candidate).replace(
        '@ORIGINAL_KERNELS@', kernels)
    out.mkdir(parents=True, exist_ok=False)
    (out / 'baseline.cu').write_text(baseline)
    (out / 'candidate.cu').write_text(candidate)
    (out / 'compare.cu').write_text(generated)
    shutil.copy2(__file__, out / Path(__file__).name)
    shutil.copy2(template, out / template.name)
    command = ['/usr/local/cuda-13.3/bin/nvcc', '-std=c++23', '-O3',
               '-arch=sm_110a', '-ccbin=g++-14', '-Xcompiler=-Wall,-Wextra',
               '-I' + str(ROOT / 'include'), str(out / 'compare.cu'),
               str(ROOT / 'build/libq4t_quant.a'), '-lcublasLt',
               '-o', str(out / 'compare')]
    (out / 'manifest.json').write_text(json.dumps({
        'binary_sha256': digest, 'baseline_ref': args.baseline_ref,
        'candidate_sha256': hashlib.sha256(candidate.encode()).hexdigest(),
        'build_command': command, 'run_command': [str(out / 'compare')],
    }, indent=2))
    with (out / 'build.log').open('w') as log:
        subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
    if 'warning' in (out / 'build.log').read_text().lower():
        raise RuntimeError('build warning')
    with (out / 'run.log').open('w') as log, (out / 'stderr.log').open('w') as err:
        subprocess.run([str(out / 'compare')], stdout=log, stderr=err, check=True)
    result = json.loads((out / 'run.log').read_text())
    if result['cases'] != 12 or result['mismatches'] or result['nonfinite']:
        raise RuntimeError('unexpected comparison results')
    (out / 'results.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()

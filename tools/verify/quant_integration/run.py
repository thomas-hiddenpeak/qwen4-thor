"""Extract production quant kernels verbatim and compare with native FP8 reference."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[3]


def function(source, marker):
    start = source.index(marker)
    brace = source.index('{', start)
    depth = 1
    end = brace + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end] + '\n'


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--header', type=Path)
    p.add_argument('--captures', type=Path, nargs=2)
    p.add_argument('--expect-failure', action='store_true')
    args = p.parse_args()
    out = args.output.resolve()
    assert any(out.is_relative_to(ROOT / d) for d in ['build', '.q4t-work'])
    out.mkdir(parents=True, exist_ok=False)
    (out / 'run.py').write_bytes(Path(__file__).read_bytes())
    (out / 'harness.cu.in').write_bytes(Path(__file__).with_name('harness.cu.in').read_bytes())
    files = ['src/quant/act_quant.cu', 'src/quant/moe_gemm.cu',
             'src/quant/moe_decode.cu', 'include/q4t/quant/format.h']
    binding = {}
    sources = []
    for name in files:
        data = (args.header if args.header and name.endswith('format.h') else ROOT / name).read_bytes()
        binding[name] = hashlib.sha256(data).hexdigest()
        dest = out / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        sources.append(data.decode())
    act, moe, dec, _ = sources
    code = '#include <cuda_bf16.h>\n#include <cuda_fp8.h>\n#include <cuda_runtime.h>\n#include "q4t/quant/format.h"\nusing namespace q4t::quant;\n'
    code += function(act, '__device__ __forceinline__ size_t SfOffsetDev')
    code += function(dec, '__device__ float Bf16ToFloat')
    code += function(act, '__global__ void ActQuantKernel')
    code += function(moe, '__global__ void GatherQuantKernel')
    code += function(moe, '__global__ void SwiGLUQuantKernel')
    code += dec[dec.index('constexpr int kSlots'):dec.index('__device__ float Bf16ToFloat')]
    code += function(dec, 'template <bool Down>\n__global__ void PrepareMoEDecodeKernel')
    code += Path(__file__).with_name('harness.cu.in').read_text()
    (out / 'check.cu').write_text(code)
    command = ['nvcc', '-std=c++23', '-arch=sm_110a', '-ccbin=g++-14',
               '-Xcompiler=-Wall,-Wextra', '-I' + str(out / 'include'),
               str(out / 'check.cu'), '-o', str(out / 'check')]
    (out / 'command.json').write_text(json.dumps(command, indent=2))
    with (out / 'build.log').open('w') as f:
        subprocess.run(command, stdout=f, stderr=subprocess.STDOUT, check=True)
    assert not (out / 'build.log').read_text().strip(), 'compiler diagnostics'
    run_command = [str(out / 'check')]
    for i, path in enumerate(args.captures or []):
        data = path.read_bytes()
        binding[str(path)] = hashlib.sha256(data).hexdigest()
        dest = out / f'capture-{i}.bf16'
        dest.write_bytes(data)
        run_command.append(str(dest))
    with (out / 'run.log').open('w') as f:
        result = subprocess.run(run_command, stdout=f, stderr=subprocess.STDOUT)
    print((out / 'run.log').read_text(), flush=True)
    (out / 'summary.json').write_text(json.dumps({'exit': result.returncode,
        'sources': binding, 'scope': 'Extracted production kernel encoding/layout; finite synthetic inputs; not GEMM or whole model'}, indent=2))
    assert result.returncode == (1 if args.expect_failure else 0)


if __name__ == '__main__':
    main()

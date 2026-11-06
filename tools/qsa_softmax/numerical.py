"""Build/run only the concrete first-HTTP FMA failure's bounded differential."""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('qsa_stage',
    Path(__file__).with_name('stage.py'))
stage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage)
spec = importlib.util.spec_from_file_location('qsa_extract',
    ROOT / 'tools/verify/compare_sparse_attention_layout.py')
extract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(extract)
BASE = ROOT / '.q4t-work/qsa-softmax-repair-20261106/contract'


def build():
    failure = json.loads((ROOT /
        '.q4t-work/qsa-softmax-20261106/failure-review.json').read_text())
    stage.require(failure['status'] == 'OUTPUT_CONTRACT_FAILED' and
                  failure['actual_http'] == 20, 'requires actual HTTP failure')
    BASE.mkdir(parents=True, exist_ok=False)
    header = (ROOT / 'include/q4t/model/full_attention.h').read_text()
    source = '#include <cuda_runtime.h>\n#include <cuda_bf16.h>\n#include <array>\n'
    source += '#include <cstdint>\n#include <cstdio>\n#include <cstring>\n#include <vector>\n'
    identities = {}
    for name, revision in [('parent', '5d641ea4'), ('failed', '15680d73'),
                           ('repaired', None)]:
        raw = (subprocess.check_output(['git', 'show',
                revision + ':src/model/full_attention.cu'], cwd=ROOT, text=True)
               if revision else (ROOT / 'src/model/full_attention.cu').read_text())
        path = BASE / (name + '.cu.txt')
        path.write_text(raw)
        identities[name] = stage.sha(path)
        source += extract.kernel_source(raw, header, name)
    source += Path(__file__).with_name('fma_contract.cu.in').read_text()
    (BASE / 'comparison.cu').write_text(source)
    command = ['/usr/local/cuda-13.3/bin/nvcc', '-std=c++23',
        '--expt-relaxed-constexpr', '-O3', '-arch=sm_110a', '-ccbin',
        '/usr/bin/g++-14', '-Xcompiler=-Wall,-Wextra', str(BASE / 'comparison.cu'),
        '-o', str(BASE / 'comparison')]
    with (BASE / 'build.log').open('x') as log:
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
    stage.save(BASE / 'build.json', dict(command=command, returncode=result.returncode,
        sources=identities, plan='QSA_SOFTMAX_PLAN_2026-11-06#failure-repair'))
    result.check_returncode()
    stage.require('warning:' not in (BASE / 'build.log').read_text(), 'build warning')
    files = [p for p in BASE.iterdir() if p.is_file()]
    files += [Path(__file__), Path(__file__).with_name('fma_contract.cu.in'),
              Path(__file__).with_name('stage.py'), Path(extract.__file__),
              ROOT / 'docs/QSA_SOFTMAX_PLAN_2026-11-06.md']
    stage.save(BASE / 'files.json', {str(p.resolve()): stage.sha(p) for p in files})
    print(json.dumps(dict(built=True, sources=identities)))


def run():
    stage.legacy.require_guard()
    seal = json.loads((BASE / 'files.json').read_text())
    stage.require(all(stage.sha(Path(n)) == h for n, h in seal.items()), 'contract seal')
    stage.require(stage.sha(ROOT / 'src/model/full_attention.cu') ==
                  stage.sha(BASE / 'repaired.cu.txt'), 'repair source changed')
    started = time.monotonic()
    with (BASE / 'result.log').open('x') as log:
        result = subprocess.run([str(BASE / 'comparison')], stdout=log,
            stderr=subprocess.STDOUT, timeout=110, env=stage.legacy.clean_env())
    raw = (BASE / 'result.log').read_text()
    stage.save(BASE / 'result.json', dict(returncode=result.returncode,
        elapsed_seconds=time.monotonic()-started, passed=result.returncode == 0 and
        raw.count('PASS ') == 2 and 'FAIL ' not in raw, model_loads=0, http_requests=0))
    print(raw, end='')
    result.check_returncode()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-only', action='store_true')
    args = parser.parse_args()
    build() if args.build_only else run()

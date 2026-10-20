"""Bounded real layer payload faults, always inside the caller's GPU lease."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parents[3]
STAGE = ROOT / '.q4t-work/offload-foundation-20261020'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--fault-only', action='store_true')
    ap.add_argument('--skip-host', action='store_true')
    args = ap.parse_args()
    out = args.output.resolve()
    assert out.is_relative_to(ROOT / '.q4t-work')
    out.mkdir(exist_ok=False)
    def sha(p):
        return hashlib.sha256(p.read_bytes()).hexdigest()
    paths = [ROOT/'src/quant/moe_residency.cpp',
             ROOT/'include/q4t/quant/moe_residency.h',
             ROOT/'tests/quant_moe_residency_test.cpp', Path(__file__),
             ROOT/'tools/verify/inference_metadata/payload_fault.cpp.in']
    sources = {}
    for p in paths:
        target = out/'source'/p.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
        sources[str(p.relative_to(ROOT))] = sha(p)
    (out/'sources.json').write_text(json.dumps(sources, indent=2))
    for name in ('q4t_tests', 'q4t_host_tests', 'CMakeCache.txt'):
        shutil.copy2(STAGE/'build'/name, out/name)
    (out/'binaries.json').write_text(json.dumps({n:sha(out/n) for n in
        ('q4t_tests','q4t_host_tests','CMakeCache.txt')}, indent=2))
    state = dict(started=time.time(), completed=[], status='RUNNING')
    env = {k:v for k,v in os.environ.items() if not k.startswith('Q4T_') and k!='LD_PRELOAD'}
    def run(name, command, extra=None):
        with (out/(name+'.log')).open('w') as log:
            subprocess.run(command, cwd=ROOT, env={**env, **(extra or {})},
                stdout=log, stderr=subprocess.STDOUT, timeout=300, check=True)
        state['completed'].append(name)
    try:
        library = out/'payload_fault.so'
        run('shim-build', ['g++-14','-std=c++23','-Wall','-Wextra','-Werror',
            '-shared','-fPIC','-I/usr/local/cuda/include','-x','c++',
            str(out/'source/tools/verify/inference_metadata/payload_fault.cpp.in'),
            '-ldl','-o',str(library)])
        if not args.fault_only and not args.skip_host:
            run('host', [str(out/'q4t_host_tests'),'--required-list',
                        'tools/release/required_host_tests.txt'])
        run('payload-faults', [str(out/'q4t_tests'),'residency_partial_payload_'],
            {'LD_PRELOAD':str(library)})
        names=['residency_load_matches_reference','residency_pread_merge_dispatch_contract',
               'residency_resolve_hit_miss_evict','residency_l2_cache_hit_evict',
               'residency_mirror_ring_hit']
        (out/'required.txt').write_text('\n'.join(names)+'\n')
        if not args.fault_only:
            run('residency', [str(out/'q4t_tests'),'--required-list',str(out/'required.txt')])
        for p in paths:
            assert sha(p)==sources[str(p.relative_to(ROOT))]
        state['status']='PASS'
    finally:
        state['finished']=time.time()
        (out/'result.json').write_text(json.dumps(state,indent=2))


if __name__=='__main__':
    main()

"""CPU fake-CUDA lifecycle/forwarding tests; no CUDA driver or GPU required."""
import json
import os
from pathlib import Path
import resource
import subprocess

root = Path(__file__).resolve().parents[2]
out = root / '.q4t-work/hc-up-layout-intervention-20261017'
out.mkdir(parents=True, exist_ok=True)
fixtures = root / 'tools/evalscope/fixtures/scratch_layout'
flags = ['g++-14', '-std=c++23', '-Wall', '-Wextra', '-Werror', '-O2',
         '-I/usr/local/cuda/include']
commands = [
    flags + ['-shared', '-fPIC', str(root / 'tools/evalscope/scratch_layout_probe.cpp'),
             '-ldl', '-o', str(out / 'probe.so')],
    flags + ['-shared', '-fPIC', str(fixtures / 'fake.cpp'), '-o', str(out / 'fake.so')],
    flags + [str(fixtures / 'driver.cpp'), str(out / 'fake.so'), '-pthread',
             '-o', str(out / 'test')],
]
for cmd in commands:
    subprocess.run(cmd, check=True)
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
rows = []
for mode in ['control', 'fixed']:
    for case in ['success', 'shape', 'stream', 'foreign']:
        env = os.environ.copy()
        env['LD_PRELOAD'] = str(out / 'probe.so')
        env['Q4T_DIAG_SCRATCH_LAYOUT'] = mode
        cmd = [str(out / 'test')] + ([] if case == 'success' else [case])
        p = subprocess.run(cmd, env=env, text=True, capture_output=True)
        (out / f'{mode}-{case}.log').write_text(p.stdout + p.stderr)
        assert p.returncode == (0 if case == 'success' else -6), p.stderr
        expected = ('complete allocations=6600 frees=6600' if case == 'success'
                    else '[scratch-layout] INVALID')
        assert expected in p.stderr
        rows.append({'mode': mode, 'case': case, 'exit': p.returncode,
                     'stdout': p.stdout, 'stderr': p.stderr})
(out / 'cpu-results.json').write_text(json.dumps(rows, indent=2) + '\n')
print('Eight CPU lifecycle/forwarding checks passed; no GPU calls.')

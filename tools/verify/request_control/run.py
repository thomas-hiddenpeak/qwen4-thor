"""Build and run host cancellation races against an exact header snapshot."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
ROOT = Path(__file__).resolve().parents[3]
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--output', type=Path, required=True)
a = p.parse_args()
out = a.output.resolve()
assert any(out.is_relative_to(ROOT / x) for x in ['build', '.q4t-work'])
out.mkdir(parents=True, exist_ok=False)
for source in [Path(__file__), Path(__file__).with_name('check.cpp')]:
    shutil.copy2(source, out / source.name)
header = out / 'include/q4t/server/request_control.h'
header.parent.mkdir(parents=True)
shutil.copy2(ROOT / 'include/q4t/server/request_control.h', header)
cmd = ['g++-14', '-std=c++23', '-Wall', '-Wextra', '-pthread', '-I' + str(out / 'include'), str(out / 'check.cpp'), '-o', str(out / 'check')]
(out / 'binding.json').write_text(json.dumps({'header_sha256': hashlib.sha256(header.read_bytes()).hexdigest(), 'command': cmd}, indent=2))
with (out / 'build.log').open('w') as log:
    subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, check=True)
assert 'warning' not in (out / 'build.log').read_text().lower()
with (out / 'run.log').open('w') as log:
    r = subprocess.run([str(out / 'check')], stdout=log, stderr=subprocess.STDOUT)
(out / 'exit.json').write_text(json.dumps({'returncode': r.returncode, 'completion_cancel_races': 2000}))
raise SystemExit(r.returncode)

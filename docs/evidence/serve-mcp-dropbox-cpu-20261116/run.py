import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
WORK = Path(__file__).resolve().parent
name, *argv = sys.argv[1:]
def protected():
    return {str(p.relative_to(ROOT)): dict(path=str(p.resolve()),
        sha256=hashlib.sha256(p.read_bytes()).hexdigest())
        for p in [ROOT/'build/q4t', ROOT/'build/q4t.release.json']}
before = protected()
started = time.monotonic()
env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1',
           Q4T_TEST_OUTPUT_DIR=str(WORK/'cpu-01'))
(WORK/'cpu-01').mkdir(exist_ok=True)
with (WORK/(name+'.stdout')).open('x') as out, (WORK/(name+'.stderr')).open('x') as err:
    result = subprocess.run(argv, cwd=ROOT, env=env, stdout=out, stderr=err,
                            timeout=1200)
after = protected()
record = dict(argv=argv, exit_code=result.returncode,
              seconds=time.monotonic()-started, protected_before=before,
              protected_after=after, default_unchanged=before == after)
(WORK/(name+'.json')).write_text(json.dumps(record, indent=2)+'\n')
print(json.dumps(record))
raise SystemExit(result.returncode if before == after else 3)

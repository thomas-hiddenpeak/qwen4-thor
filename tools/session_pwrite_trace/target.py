"""Bind the inherited PID before exec, so cache file opens are observable."""
import json
import os
from pathlib import Path
import sys
import time

out = Path(sys.argv[1])
deadline = float(sys.argv[2])
tmp = out/'target.tmp'
tmp.write_text(json.dumps({'pid': os.getpid()})+'\n')
tmp.replace(out/'target.json')
while True:
    ready = out/'target-ready.json'
    if ready.exists() and json.loads(ready.read_text()).get('pid') == os.getpid():
        break
    if time.time() >= deadline:
        raise TimeoutError('trace target binding')
    if (out/'exit.json').exists():
        raise RuntimeError('collector exited before binding')
    time.sleep(.01)
os.execv(sys.argv[3], sys.argv[3:])

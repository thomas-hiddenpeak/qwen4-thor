import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.native_tts import acceptance as gate
out = Path(__file__).parent
lease = int(os.environ['Q4T_MANAGED_LEASE_FD'])
os.fstat(lease)
started = time.monotonic()
plan = json.loads((out / 'plan.json').read_text())
before = gate.defaults()
result = dict(status='RUNNING', defaults_before=before, runs=[], plan=plan,
              binary_sha256=gate.digest(out / 'replay'))
env = dict(os.environ, PYTHONPATH=str(ROOT / '.q4t-work/serve-audio-native-20261117/reference-deps'),
           HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', PYTHONDONTWRITEBYTECODE='1')
try:
    commands = [('reference', [str(ROOT / '.q4t-work/refenv/bin/python'), '-B',
                               str(out / 'reference.py')]),
                ('native', [str(out / 'replay'), str(out.parent / 'assets-1b7-01'),
                            str(out / 'reference/projected.bf16'), str(out / 'native')])]
    for name, command in commands:
        t = time.monotonic()
        with (out / f'{name}.log').open('wb') as log:
            proc = subprocess.run(command, env=env, pass_fds=(lease,),
                stdout=log, stderr=subprocess.STDOUT,
                timeout=min(60,120-(time.monotonic()-started)))
        result['runs'].append(dict(name=name,code=proc.returncode,seconds=time.monotonic()-t))
        assert proc.returncode == 0, name+' failed'
    result['checks'] = []
    for a in sorted((out/'native').glob('diag-*.bf16')):
        b = out/'reference'/a.name
        result['checks'].append(dict(operand=a.name,
            **gate.compare_pair(a,b,a.stat().st_size//2,gate.THRESHOLDS['dense'])))
    result['status']='DIAGNOSIS_ONLY'
except Exception as error:
    result.update(status='FAILED_DIAGNOSIS', error=str(error))
finally:
    result['defaults_unchanged'] = before == gate.defaults()
    result['seconds'] = time.monotonic()-started
    (out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
assert result['status']=='DIAGNOSIS_ONLY' and result['defaults_unchanged']

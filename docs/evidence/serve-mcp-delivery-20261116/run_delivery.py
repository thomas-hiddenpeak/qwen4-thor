import json,subprocess,sys,time
from pathlib import Path
sys.dont_write_bytecode=True
W=Path(__file__).resolve().parent
argv=[sys.executable,'-B',str(W/'deliver.py'),'run']
started=time.monotonic()
with (W/'delivery-run-01.stdout').open('x') as out,(W/'delivery-run-01.stderr').open('x') as err:
 result=subprocess.run(argv,stdout=out,stderr=err,timeout=600)
record=dict(argv=argv,returncode=result.returncode,seconds=time.monotonic()-started,
 default_change_authorized=True,new_gpu_starts=0)
(W/'delivery-run-01.json').write_text(json.dumps(record,indent=2)+'\n')
print(json.dumps(record))
raise SystemExit(result.returncode)

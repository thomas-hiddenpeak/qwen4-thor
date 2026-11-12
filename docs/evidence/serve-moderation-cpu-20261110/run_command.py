import json, subprocess, sys, time
from pathlib import Path
work=Path(__file__).resolve().parent
name,*argv=sys.argv[1:]
start=time.monotonic()
with (work/(name+'.stdout')).open('x') as out, (work/(name+'.stderr')).open('x') as err:
    result=subprocess.run(argv,stdout=out,stderr=err)
record=dict(argv=argv,exit_code=result.returncode,seconds=time.monotonic()-start)
(work/(name+'.json')).write_text(json.dumps(record,indent=2)+'\n')
print(json.dumps(record),flush=True)
raise SystemExit(result.returncode)

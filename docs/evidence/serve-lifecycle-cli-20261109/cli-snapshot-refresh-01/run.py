import json,subprocess,time,sys
from pathlib import Path
out=Path('.q4t-work/serve-lifecycle-20261109/cli-snapshot-refresh-01');commands=json.loads((out/'commands.json').read_text());rows=[]
for i,command in enumerate(commands):
 with (out/(str(i)+'.stdout.txt')).open('x') as stdout,(out/(str(i)+'.stderr.txt')).open('x') as stderr:r=subprocess.run(command,stdout=stdout,stderr=stderr)
 rows.append(dict(command=command,returncode=r.returncode))
 if r.returncode:break
(out/'result.json').write_text(json.dumps(rows,indent=2)+'\n');print(json.dumps(rows));sys.exit(rows[-1]['returncode'])

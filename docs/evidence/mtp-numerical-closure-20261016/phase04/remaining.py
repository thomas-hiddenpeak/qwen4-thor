import json, os, select, subprocess
from pathlib import Path
root=Path('/home/rm01/models/dev/qwen4-thor')
r=Path(__file__).resolve().parent
# Wait for this specific live matrix process, not a stale lock or PID reuse.
fd=os.pidfd_open(956349)
select.select([fd],[],[])
os.close(fd)
for name in ['off','on','quality-off','quality-on']:
    result=json.loads((r/(name+'-exit.json')).read_text())
    if result['returncode'] != 0:
        raise SystemExit('matrix failed: '+name)
for name,cmd in [
    ('boundaries',['python3',str(r/'boundaries.py')]),
    ('final-test-build',['cmake','--build','build','--target','q4t_tests','--parallel','4']),
    ('analysis',['python3',str(r/'analyze.py')]),
]:
    with (r/(name+'-driver.log')).open('w') as log:
        result=subprocess.run(cmd,cwd=root,stdout=log,stderr=subprocess.STDOUT)
    (r/(name+'-exit.json')).write_text(json.dumps({'returncode':result.returncode,'command':cmd},indent=2)+'\n')
    if result.returncode:
        raise SystemExit(result.returncode)

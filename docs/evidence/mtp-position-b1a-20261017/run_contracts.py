import subprocess,json,hashlib
from pathlib import Path
root=Path('/home/rm01/models/dev/qwen4-thor')
out=Path(__file__).resolve().parent
assert (out/'http-complete').read_text().strip()=='passed'
# Only test fixtures changed during HTTP. Build after all performance requests
# so compilation cannot interfere with their CPU scheduling or memory traffic.
with (out/'build-contracts-post-http.log').open('w') as log:
    build=subprocess.run(['cmake','--build','build','--target','q4t_tests',
                          '--parallel','2'],cwd=root,stdout=log,
                          stderr=subprocess.STDOUT)
if build.returncode or 'warning:' in (out/'build-contracts-post-http.log').read_text():
    raise SystemExit('contract build failed or warned')
(out/'contracts-identity-executed.json').write_text(json.dumps({
    f:hashlib.sha256((root/f).read_bytes()).hexdigest()
    for f in ['build/q4t_tests','tests/mtp_position_metadata_test.cpp',
              'tests/mtp_spec_multi_test.cpp','src/mtp/mtp.cu']},indent=2)+'\n')
results=[]
for name,cmd in [('contracts',[str(root/'build/q4t_tests'),'--required-list',str(out/'contracts.required')]),('boundaries',['python3',str(out/'boundaries.py')])]:
    with (out/(name+'.log')).open('w') as log:
        p=subprocess.run(cmd,cwd=root,stdout=log,stderr=subprocess.STDOUT)
    results.append({'name':name,'command':cmd,'returncode':p.returncode})
    (out/'direct-exit.json').write_text(json.dumps(results,indent=2)+'\n')
    if p.returncode:raise SystemExit(p.returncode)

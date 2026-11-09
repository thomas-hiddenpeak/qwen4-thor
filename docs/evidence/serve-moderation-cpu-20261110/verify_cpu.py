import hashlib, json, os, subprocess, time
from pathlib import Path
root=Path('/home/rm01/models/dev/qwen4-thor')
work=root/'.q4t-work/serve-moderation-20261110'
out=work/'cpu-01'
out.mkdir(exist_ok=False)
for name in ('host-build-02','engine-build-02'):
    result=json.loads((work/(name+'.json')).read_text())
    assert result['exit_code']==0, result
    assert 'warning:' not in (work/(name+'.stdout')).read_text()+(work/(name+'.stderr')).read_text()
env=dict(os.environ, Q4T_TEST_OUTPUT_DIR=str(out), PYTHONDONTWRITEBYTECODE='1')
commands=[
 ('host',[str(root/'.q4t-work/serve-access-20261109/host/q4t_host_tests'),'--required-list',str(work/'required-cpu.txt')]),
 ('schema',['python3','-B','-m','unittest','tools.serve_responses.test_moderation_contract','-v']),
 ('inventory',['python3','-B','tools/serve_audit/check_inventory.py','--self-check']),
 ('application',['python3','-B','tools/serve_responses/build_contract.py','--types',str(root/'.q4t-work/serve-prompt-cache-20261110/application-types.json'),'--check']),
 ('protocol',['python3','-B','tools/serve_protocol/build_contract.py','--check']),
 ('whitespace',['git','diff','--check'])]
with (out/'source.diff').open('w') as f:subprocess.run(['git','diff','--binary'],cwd=root,stdout=f,check=True)
paths=subprocess.check_output(['git','ls-files','--modified','--others','--exclude-standard'],cwd=root,text=True).splitlines()
(out/'source-sha256.json').write_text(json.dumps({p:hashlib.sha256((root/p).read_bytes()).hexdigest() for p in paths},indent=2)+'\n')
results=[]
for name,argv in commands:
    start=time.monotonic()
    with (out/(name+'.stdout')).open('w') as stdout,(out/(name+'.stderr')).open('w') as stderr:
        r=subprocess.run(argv,cwd=root,env=env,stdout=stdout,stderr=stderr,timeout=180)
    results.append(dict(name=name,argv=argv,exit_code=r.returncode,seconds=time.monotonic()-start))
    (out/'results.json').write_text(json.dumps(results,indent=2)+'\n')
    print(json.dumps(results[-1]),flush=True)
    if r.returncode:raise SystemExit(r.returncode)

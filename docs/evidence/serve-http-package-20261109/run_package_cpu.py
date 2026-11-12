import hashlib,json,os,subprocess,sys,time
from pathlib import Path
root=Path.cwd();base=root/'.q4t-work/serve-http-20261109';out=base/'package-cpu-01';out.mkdir()
plan=json.loads((base/'package-plan.json').read_bytes());cmd=[sys.executable,'-B','-m','unittest','-v',*plan['tests']]
(out/'command.json').write_text(json.dumps(cmd,indent=2)+'\n')
files=subprocess.check_output(['git','ls-files','tools/serve_recovery','tools/serve_gateway','tools/serve_service'],text=True).splitlines()
(out/'sources.json').write_text(json.dumps({n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in files if n.endswith('.py')},indent=2)+'\n')
start=time.monotonic();code=125
try:
 with (out/'tests.log').open('wb') as log:code=subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT,timeout=120,env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',Q4T_TEST_OUTPUT_DIR=str(out/'work'))).returncode
finally:
 result=dict(exit_code=code,seconds=time.monotonic()-start,tests=len(plan['tests']),gpu_operations=0)
 (out/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result));print((out/'tests.log').read_text()[-4500:])

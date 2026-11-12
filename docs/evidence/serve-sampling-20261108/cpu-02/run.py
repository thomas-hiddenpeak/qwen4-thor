from pathlib import Path
import json,os,subprocess,time
root=Path.cwd();out=root/'.q4t-work/serve-sampling-20261108/cpu-02';build=root/'.q4t-work/serve-sampling-20261108/engine-build'
rows={
 'build':['cmake','--build',str(build),'--target','q4t_host_tests','--parallel','4'],
 'host':[str(build/'q4t_host_tests'),'--required-list',str(out/'host-required.txt')],
 'python':['python3','-m','unittest','tools.examples.test_chat_client']}
result={};env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',PYTHONPATH=str(root/'tools/examples')+':'+str(root))
for name,cmd in rows.items():
 start=time.monotonic()
 with (out/(name+'.log')).open('x') as log:run=subprocess.run(cmd,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=120)
 result[name]=dict(argv=cmd,exit_code=run.returncode,seconds=time.monotonic()-start)
 if run.returncode:break
(out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({key:value['exit_code'] for key,value in result.items()}))
raise SystemExit(any(r['exit_code'] for r in result.values()))

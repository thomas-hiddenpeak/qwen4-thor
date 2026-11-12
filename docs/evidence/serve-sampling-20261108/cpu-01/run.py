from pathlib import Path
import concurrent.futures, json, os, subprocess, time
root=Path.cwd(); out=root/'.q4t-work/serve-sampling-20261108/cpu-01'; build=root/'.q4t-work/serve-sampling-20261108/engine-build'
plan=json.loads((out/'plan.json').read_text());env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',Q4T_NODE_SDK_DIR=plan['sdk']['sdk_directory'])
rows={
 'host':[str(build/'q4t_host_tests'),'--required-list',str(out/'host-required.txt')],
 'shapes':['python3','tools/serve_protocol/check_contract.py','--probe',str(build/'q4t_contract_probe'),'--output',str(out/'shapes'),'--case-list',str(out/'contract-required.txt')],
 'python':['python3','-m','unittest','tools.serve_sampling.test_clients','tools.examples.test_chat_client'],
 'js':['node','--test','tools/examples/js/test_chat_client.mjs'],
 'schema':['python3','tools/serve_protocol/build_contract.py','--check'],
 'sources':['python3','tools/serve_audit/check_inventory.py','--self-check']}
def run(item):
 name,argv=item; start=time.monotonic()
 with (out/(name+'.log')).open('x') as log:
  result=subprocess.run(argv,cwd=root,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=180)
 return name,dict(argv=argv,exit_code=result.returncode,seconds=time.monotonic()-start)
with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
 result=dict(pool.map(run,rows.items()))
(out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({key:value['exit_code'] for key,value in result.items()}),flush=True)
raise SystemExit(any(row['exit_code'] for row in result.values()))

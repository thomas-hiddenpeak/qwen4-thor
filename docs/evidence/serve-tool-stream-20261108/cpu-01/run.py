from pathlib import Path
import concurrent.futures,json,os,subprocess,time
root=Path.cwd();base=root/'.q4t-work/serve-tool-stream-20261108';out=base/'cpu-01';build=base/'engine-build'
plan=json.loads((out/'plan.json').read_text())
env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',Q4T_NODE_SDK_DIR=str(root/'.q4t-work/serve-protocol-20261103/sdk'),Q4T_PREDICTION_PROBE=str(build/'q4t_contract_probe'))
steps={'host':[str(build/'q4t_host_tests'),'--required-list',str(out/'host-required.txt')],
 'shapes':['python3','tools/serve_protocol/check_contract.py','--probe',str(build/'q4t_contract_probe'),'--output',str(out/'shapes'),'--case-list',str(out/'contract-required.txt')],
 'python':['python3','-m','unittest',*plan['python']],
 'js':['node','--test','--test-name-pattern='+plan['js_pattern'],'tools/examples/js/test_chat_client.mjs'],
 'schema':['python3','tools/serve_protocol/build_contract.py','--check'],
 'sources':['python3','tools/serve_audit/check_inventory.py','--self-check']}
def run(item):
 name,argv=item;start=time.monotonic()
 with (out/(name+'.log')).open('x') as log:r=subprocess.run(argv,cwd=root,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=180)
 return name,dict(argv=argv,exit_code=r.returncode,seconds=time.monotonic()-start)
with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:result=dict(pool.map(run,steps.items()))
(out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({k:v['exit_code'] for k,v in result.items()}),flush=True)
raise SystemExit(any(v['exit_code'] for v in result.values()))

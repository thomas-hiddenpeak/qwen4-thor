import json,os,pathlib,subprocess,sys,time
root=pathlib.Path('/home/rm01/models/dev/qwen4-thor')
base=root/'.q4t-work/main-reaudit-fixes-20261031'
kind=sys.argv[1]; label=sys.argv[2] if len(sys.argv)>2 else kind
if kind=='io':
 b=base/'io-host'; cmds=[['cmake','-S',str(root/'tests/io_host'),'-B',str(b),'-DCMAKE_BUILD_TYPE=Release','-DCMAKE_CXX_COMPILER=g++-14','-DQ4T_BUILD_RESIDENCY_CUDA_CONTRACTS=ON'],['cmake','--build',str(b),'--parallel','4'],['ctest','--test-dir',str(b),'--output-on-failure','--verbose']]
elif kind=='host':
 b=base/'host'; cmds=[['cmake','-S',str(root/'tests/host'),'-B',str(b),'-DCMAKE_BUILD_TYPE=Release','-DCMAKE_CXX_COMPILER=g++-14','-DPython3_EXECUTABLE=/usr/bin/python3'],['cmake','--build',str(b),'--parallel','4'],['ctest','--test-dir',str(b),'--output-on-failure','--verbose']]
elif kind=='full':
 b=base/'build'; cmds=[['cmake','-S',str(root),'-B',str(b),'-DCMAKE_BUILD_TYPE=Release','-DCMAKE_CXX_COMPILER=g++-14','-DCMAKE_CUDA_HOST_COMPILER=g++-14','-DQ4T_CUDA_ARCHITECTURES=110a'],['cmake','--build',str(b),'--parallel','6']]
elif kind=='budget':
 b=base/'budget-host'; cmds=[['cmake','-S',str(root/'tests/budget_host'),'-B',str(b),'-DCMAKE_BUILD_TYPE=Release','-DCMAKE_CXX_COMPILER=g++-14'],['cmake','--build',str(b),'--parallel','4'],['ctest','--test-dir',str(b),'--output-on-failure','--verbose']]
elif kind=='supervisor':
 cmds=[['/usr/bin/python3','-B',str(root/'tools/release/test_supervisor.py'),'-v']]
else: raise RuntimeError(kind)
records=[]
env=os.environ|{'PYTHONDONTWRITEBYTECODE':'1','Q4T_TEST_OUTPUT_DIR':str(base/'supervisor-fixtures')}
for i,cmd in enumerate(cmds):
 p=base/f'{label}-{i}.log'; start=time.time()
 with p.open('x') as log:
  result=subprocess.run(cmd,cwd=root,env=env,stdout=log,stderr=subprocess.STDOUT)
 row=dict(argv=cmd,log=str(p),returncode=result.returncode,seconds=time.time()-start)
 records.append(row)
 (base/f'{label}-commands.json').write_text(json.dumps(records,indent=2)+'\n')
 print(json.dumps(row),flush=True)
 if result.returncode:
  print(p.read_text()[-12000:],flush=True);sys.exit(result.returncode)

import hashlib,json,os,subprocess,sys,time
from pathlib import Path
sys.dont_write_bytecode=True
root=Path.cwd();base=root/'.q4t-work/serve-gateway-diagnostics-20261109';out=base/'cpu-01';build=root/'.q4t-work/serve-custom-tools-20261108/engine-build'
read=lambda p:json.loads(p.read_text());save=lambda p,v:p.write_text(json.dumps(v,indent=2)+'\n');sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
plan=read(out/'plan.json');assert not (out/'result.json').exists()
assert all(sha(root/n)==h for n,h in plan['sources'].items())
protected={str(p):sha(p) for p in (root/'build/q4t',root/'build/q4t.release.json',root/'build/service.release.json',root/'build/service-native-state/ledger.json')}
save(out/'protected.json',protected)
command=['cmake','--build',str(build),'--target','q4t_guardian','--parallel','4']
start=time.monotonic();built=subprocess.run(command,capture_output=True,timeout=180)
(out/'build.stdout').write_bytes(built.stdout);(out/'build.stderr').write_bytes(built.stderr);save(out/'build.json',dict(argv=command,returncode=built.returncode,seconds=time.monotonic()-start))
assert built.returncode==0 and b'warning:' not in built.stdout+built.stderr,'build failed or warning'
command=[sys.executable,'-B','-m','unittest','-v',*plan['tests']]
env=dict(os.environ,Q4T_GUARDIAN_BINARY=str(build/'q4t-guardian'),Q4T_TEST_OUTPUT_DIR=str(out/'runtime'))
start=time.monotonic();tested=subprocess.run(command,capture_output=True,timeout=180,env=env)
(out/'tests.stdout').write_bytes(tested.stdout);(out/'tests.stderr').write_bytes(tested.stderr)
assert all(sha(Path(p))==h for p,h in protected.items()),'default changed'
result=dict(status='PASS_TARGETED_GATEWAY_DIAGNOSTICS' if tested.returncode==0 else 'FAILED',cases=plan['cases'],argv=command,returncode=tested.returncode,seconds=time.monotonic()-start,guardian_sha256=sha(build/'q4t-guardian'),default_preserved=True,model_starts=0,chat_requests=0,gpu_queries=0)
save(out/'result.json',result);print(json.dumps(result));sys.exit(tested.returncode)

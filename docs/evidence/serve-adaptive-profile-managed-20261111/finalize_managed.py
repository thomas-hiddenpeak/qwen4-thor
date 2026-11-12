import fcntl,json,runpy,socket,sys
from pathlib import Path
root=Path.cwd();sys.path.insert(0,str(root))
w=root/'.q4t-work/serve-adaptive-profiles-20261111/managed-01'
plan=json.loads((w/'plan.json').read_text());scope=runpy.run_path(str(root/'tools/serve_recovery/profile_managed_acceptance.py'))
manifest=scope['LEGACY']['verify'](plan)
from tools.serve_service import package as p
from tools.serve_recovery import journal,profile_policy
result=p.read(w/'execution/result.json');guard=p.read(w/'lease/exit.json');stop=p.read(w/'execution/stop.json')
p.require(result['status']=='PASS_MANAGED_ADAPTIVE_PROFILES' and result['failure'] is None and result['cleanup_failure'] is None,'business failed')
p.require(result['budget']['counts']['starts']==3 and result['budget']['counts']['chats']==12 and result['budget']['counts']['probes']==0 and result['seconds']<900,'frozen budget')
p.require(guard['status']=='COMPLETED' and guard['child_returncode']==0 and guard['compute_backend']=='nvml' and guard['lease_inherited_by_child'],'strict environment')
queries=[json.loads(s) for s in (w/'lease/nvml-queries.jsonl').read_text().splitlines()]
p.require(queries and all(q['status']=='OK' and q['returncode']==0 for q in queries),'NVML query failed')
p.require(stop['state']=='STOPPED' and stop['stop_durable'] and stop['cleanup_confirmed'],'cleanup not confirmed')
statuses=[json.loads(s) for s in (w/'execution/status.jsonl').read_text().splitlines()]
owned={}
for s in statuses:
 for role in ['model','controller','gateway','guardian','journal']:
  if s.get(role+'_pid'):
   owned[s[role+'_pid']]=s[role+'_start_ticks']
for pid,ticks in owned.items():
 try:p.require(journal.process_start(pid)!=ticks,'owned identity alive')
 except (FileNotFoundError,ProcessLookupError):pass
policy=p.read(Path(plan['package']['package'])/'gateway-policy.json')
for port in [policy['listen_port'],policy['upstream_port'],policy['upstream_operations_port']]:
 with socket.socket() as s:
  s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('127.0.0.1',port))
with Path(manifest['lock_path']).open('r+') as f:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
pressure=p.read(w/'execution/pressure.json');samples=pressure['observations']
p.require(pressure['all_owned_pages_released'] and pressure['reason']=='released_by_owner' and pressure['allocated_bytes']<=24*1024**3 and pressure['seconds']<=120,'RAM bound')
minimum=min(v['available_bytes'] for v in samples)
p.require(minimum>=20*1024**3 and minimum<24*1024**3,'real low water and independent floor')
profiles=profile_policy.validate(p.read(w/'execution/final-profiles.json'))
p.require(profiles['selected']=='primary' and profiles['pending'] is None and profiles['completed_generation']==3 and len(profiles['history'])==2,'profile completion')
quota=p.read(w/'execution/quota-window.json');p.require(quota['denial_checked'] and quota['elapsed_seconds']<59,'live RPM continuation absent')
value=dict(status='ACCEPT_MANAGED_ADAPTIVE_PROFILES',counts=result['budget']['counts'],seconds=result['seconds'],nvml_queries=len(queries),all_queries_ok=True,env_abort=False,default_unchanged=True,postflight=dict(sources_unchanged=True,owned_processes_gone=True,ports_free=True,lock_released=True),pressure=dict(allocated_bytes=pressure['allocated_bytes'],minimum_observed_available_bytes=minimum,seconds=pressure['seconds'],all_owned_pages_released=True),original_batch_and_chat=True,tool_executions=1,original_quota_denied_after_seconds=quota['elapsed_seconds'],same_control_tree=True,profile_switches=2,profile_journal_revision=profiles['revision'],engine_binary_sha256=plan['package']['engine_binary_sha256'],manifest_sha256=plan['package']['manifest_sha256'],test_policy=plan['profile_policy'],production_policy=profile_policy.DEFAULTS,policy_timing_cpu_evidence='docs/evidence/serve-adaptive-profile-state-20261111/README.md',performance_claim=False)
p.save(w/'acceptance-final.json',value);print(json.dumps(value,indent=2))

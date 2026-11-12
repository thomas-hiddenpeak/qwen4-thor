"""Accept targeted tool contracts only after strict lease and cleanup checks."""
from pathlib import Path
import fcntl,json
from tools.serve_responses import tool_identity_acceptance as run
from tools.serve_responses.background_acceptance import probe_ports
from tools.serve_service.package import read,require,save,sha
ROOT=Path.cwd();WORK=ROOT/'.q4t-work/serve-tool-namespace-20261112';OUT=WORK/'business-03'
plan,result,cleanup,summary=[read(OUT/n) for n in ['plan.json','result.json','cleanup.json','business-summary.json']]
require(result['status']==run.Stage.success_status and result['counts']['starts']==1 and result['counts']['chats']==6 and result['seconds']<900,'business/budget failed')
require(result['binary_sha256']==plan['binary_sha256']==sha(plan['binary']),'engine changed')
for name,digest in {**plan['sources'],**plan['protected']}.items():require(sha(ROOT/name)==digest,'source/default changed: '+name)
require(cleanup['remaining']==[] and cleanup['model_exit']==0 and cleanup['ports_released'] and cleanup['lease_continuously_held'],'cleanup incomplete')
processes=run.base.lifecycle.guard.processes()
require(not any(processes.get(int(pid),{}).get('start_ticks')==row['start_ticks'] for pid,row in cleanup['owned'].items()),'owned process remains')
probe_ports((plan['port'],plan['operations_port']))
with run.base.lifecycle.guard.common_lock().open('r+') as lock:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
lease=WORK/'lease-03';guard=read(lease/'exit.json');queries=[json.loads(line) for line in (lease/'nvml-queries.jsonl').read_text().splitlines()]
require(guard['status']=='COMPLETED' and guard['child_returncode']==0 and guard['lease_inherited_by_child'] and guard['compute_backend']=='nvml' and queries and all(q['status']=='OK' and q['returncode']==0 for q in queries) and queries[-1]['pids']==[],'strict GPU environment failed')
require(summary['actual_tool_executions']==2 and summary['complete_generations']==4 and summary['invalid_results']==1 and summary['incomplete_generations']==1,'business summary differs')
previous=[read(WORK/f'business-0{i}/result.json') for i in [1,2]]
require(all(r['status']=='FAILED' for r in previous),'failed history changed')
rows={str(p.relative_to(WORK)):sha(p) for folder in [OUT,lease] for p in folder.iterdir() if p.is_file()}
decision=dict(status='ACCEPT_TOOL_IDENTITY_RESULT_CONTRACT_BUSINESS',binary_sha256=result['binary_sha256'],source_commit=plan['source_commit'],protocol_sha256=read(OUT/'capabilities.json')['protocol_source_sha256'],counts=result['counts'],seconds=result['seconds'],nvml_queries=len(queries),aggregate=dict(starts=3,create_requests=10,seconds=sum(r['seconds'] for r in previous)+result['seconds']),failed_history_retained=True,summary=summary,env_abort=False,postflight=dict(sources_unchanged=True,protected_default_unchanged=True,owned_processes_gone=True,ports_free=True,lock_released=True),records=rows,performance_acceptance=False,full_model_quality=False,long_stability_accepted=False,remaining_master_tasks_open=True)
require(not (OUT/'acceptance.json').exists(),'preserve original acceptance');save(OUT/'acceptance.json',decision)
print(json.dumps({k:decision[k] for k in ['status','binary_sha256','counts','seconds','aggregate','nvml_queries']}))

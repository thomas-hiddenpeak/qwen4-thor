import collections,json,sqlite3
from pathlib import Path
ROOT=Path('/home/rm01/models/dev/qwen4-thor');OUT=Path(__file__).resolve().parent
prior=json.loads((ROOT/'.q4t-work/hc-up-trace-counts-20261017/result.json').read_text());report={}
for v in 'AB':
 tid=prior[v]['launches_without_kernel'][0]['globalTid']
 c=sqlite3.connect(f'file:{ROOT}/.q4t-work/hc-up-clean-trace-v2-20261017/{v}/context-204800/trace.sqlite?mode=ro',uri=True)
 names=dict(c.execute('select id,value from StringIds'))
 rows=list(c.execute('select start,end,nameId,returnValue from CUPTI_ACTIVITY_KIND_RUNTIME where globalTid=? order by start',(tid,)))
 counts=dict(collections.Counter(names[n] for a,b,n,rv in rows));assert counts=={'cuLaunchKernel':110}
 report[v]={'thread_id':tid,'api_counts':counts,'return_codes':dict(collections.Counter(rv for a,b,n,rv in rows)),'api_duration_sum_ns':sum(b-a for a,b,n,rv in rows),'device_completion_bound_available_from_worker_apis':False}
 c.close()
commands=[]
for p in sorted((ROOT/'.q4t-work/hc-up-review-20261016').glob('[0-9][0-9]-[AB]/server-command.json')):
 cmd=json.loads(p.read_text());assert cmd['argv'][0].endswith(('/q4t-main','/q4t'))
 commands.append({'path':str(p),'argv':cmd['argv'],'effective_q4t_environment':cmd.get('effective_q4t_environment')})
assert len(commands)==6
report['unprofiled_review_commands']=commands
report['limits']=['Worker API return is not device completion; no device-duration estimate','No Nsight wrapper in six review commands, not a complete historical process maps audit','Internal launch implementation/purpose not established from these records','Profiler artifacts cannot by themselves explain the unprofiled regression']
(OUT/'result.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({v:report[v] for v in 'AB'},indent=2))

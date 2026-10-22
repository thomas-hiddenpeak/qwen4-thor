from pathlib import Path
import hashlib,json,re
import argparse
parser=argparse.ArgumentParser()
parser.add_argument('--base',type=Path,required=True)
b=parser.parse_args().base.resolve();r=b/'source';o=r/'.q4t-work/acceptance-v2';root=b.parents[1]
def sha(p):
 with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
plan=json.loads((b/'frozen-plan-v2.json').read_text());state=json.loads((o/'state.json').read_text());assert state['status']=='MEASURED_REQUIRES_REVIEW'
assert state['completed']==[x['name'] for x in plan['steps']]
for p,h in plan['files'].items():assert sha(Path(p))==h,p
sealed=json.loads((o/'files.json').read_text())
for p,h in sealed.items():assert sha(o/p)==h,p
assert sha(root/'build/q4t')==sha(b/'parent/q4t')
assert sha(root/'build/q4t.release.json')==sha(b/'parent/original.release.json')
assert not re.search(r'warning:|error:',(b/'build-resume.log').read_text(),re.I)
guard=json.loads((b/'lease-v2/exit.json').read_text());print('guard',guard)
assert guard['status']=='COMPLETED' and guard['child_returncode']==0 and 'cleanup_error' not in guard
queries=[json.loads(l) for l in (b/'lease-v2/nvml-queries.jsonl').read_text().splitlines()];assert all(q['status']=='OK' for q in queries)
counts={};total=0
for name in ['quality-on','quality-off','parent-off','candidate-off','candidate-on','parent-on']:
 p=o/name;rows=json.loads((p/'results.json').read_text());ex=json.loads((p/'exit.json').read_text());assert ex['http_output_checks_passed'] and ex['server']==0
 count=11 if name.startswith('quality') else 15;total+=count
 log=(p/'server.log').read_text();assert len(re.findall(r'event=admitted ',log))==count
 final=[json.loads(l.split('] ',1)[1]) for l in log.splitlines() if l.startswith('[startup-reservation] ') and '"event":"finalized"' in l];assert len(final)==1 and final[0]['status']=='PASS' and final[0]['release_unknown']==0
 counts[name]={'requests':count,'finalized':final[0]}
assert total==82
result=dict(status='PASS_BOUNDED_EVIDENCE',frozen_files_verified=len(plan['files']),sealed_run_files_verified=len(sealed),http_requests=total,groups=counts,nvml_queries=len(queries),nvml_max_seconds=max(q['seconds'] for q in queries),guard=guard,elapsed_seconds=state['finished']-state['started'],build_zero_warnings=True,default_pair_unchanged_before_delivery=True,source_parent='739aff37babd99edb57bb935433aa747f7c3baf6',candidate_sha256=sha(b/'build/q4t'))
(b/'audit-v2.json').write_text(json.dumps(result,indent=2)+'\n');print('audit PASS',total,len(queries))

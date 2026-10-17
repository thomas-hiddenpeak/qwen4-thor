from pathlib import Path
import json,hashlib
root=Path('/home/rm01/models/dev/qwen4-thor')
base=root/'.q4t-work/no-mtp-baseline-20261018';old=root/'.q4t-work/b1a-20261017'
checks={};rows=[]
def read(p):
 b=p.read_bytes();checks[str(p.relative_to(root))]=hashlib.sha256(b).hexdigest();return json.loads(b)
def metric(r):return {'T':sum(x['ttft'] for x in r)/len(r),'D':sum(x['actual_output']-1 for x in r)/sum(x['latency']-x['ttft'] for x in r)}
def delta(a,b):return {'ttft_increase_pct':100*(b['T']/a['T']-1),'decode_decrease_pct':100*(1-b['D']/a['D'])}
for n in [1024,4096,8192,45056,204800]:
 candidate=read(old/f'off/context-{n}/responses.json'); b=metric(candidate)
 item={'length':n,'old_candidate_off':b,'comparisons_are_noncontemporaneous':True}
 for name in ['A0','A1']:
  raw=read(base/f'{name}/context-{n}/responses.json');a=metric(raw)
  assert len(raw)==len(candidate)==3
  for x,y in zip(raw,candidate):
   assert all(x[k]==y[k] for k in ['prompt_sha256','text','actual_input','actual_output','finish'])
  item[name]=a;item['old_candidate_vs_'+name]=delta(a,b)
 item['new_A1_vs_A0']=delta(item['A0'],item['A1']);rows.append(item)
q=read(base/'Q/results.json');bq=read(old/'quality-off/results.json')
assert len(q)==len(bq)==11
for x,y in zip(q,bq):assert all(x[k]==y[k] for k in ['id','prompt_sha256','text','actual_input','actual_output','finish'])
for p in [base/'lease/exit.json',base/'final-audit.json',base/'summary.json',old/'off/server-command.json',base/'A0/server-command.json',base/'A1/server-command.json']:read(p)
result={'status':'OFFLINE_REVIEW_ONLY','new_requests':0,'quality_reference_identity_matches':True,'performance_prompt_output_identity_matches':True,'reviewed_source_hashes':checks,'metrics':rows,'warning':'Old candidate comparisons are descriptive and noncontemporaneous. No noise band or causal inference.'}
out=root/'.q4t-work/b1a-after-baseline-review-20261018/review.json';out.write_text(json.dumps(result,indent=2)+'\n')
for r in rows:
 print(r['length'],r['old_candidate_vs_A0'],r['old_candidate_vs_A1'])

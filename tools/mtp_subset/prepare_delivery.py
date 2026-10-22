from pathlib import Path
import hashlib,json,shutil,subprocess
import argparse
parser=argparse.ArgumentParser()
parser.add_argument('--base',type=Path,required=True)
b=parser.parse_args().base.resolve();r=b/'source';root=b.parents[1]
def sha(p):
 with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def save(p,x):p.write_text(json.dumps(x,indent=2)+'\n')
a=json.loads((b/'analysis-v2.json').read_text())
assert a['state']['status']=='MEASURED_REQUIRES_REVIEW'
assert len(a['on_comparison'])==5 and all(x['decode_change_pct']>=5 and x['outputs_equal'] for x in a['on_comparison'])
assert (b/'decision-v2.json').exists() and json.loads((b/'decision-v2.json').read_text())['engineering_decision']=='ACCEPT_BOUNDED_OPT_IN'
p=b/'delivery-package-v2';p.mkdir()
for sub in ['previous','candidate','artifact','health-fixture']:(p/sub).mkdir()
for src,dest in [(root/'build/q4t',p/'previous/q4t'),(root/'build/q4t.release.json',p/'previous/q4t.release.json'),(b/'build/q4t',p/'candidate/q4t'),(b/'build/CMakeCache.txt',p/'candidate/CMakeCache.txt'),(b/'decision-v2.json',p/'decision.json'),(b/'analysis-v2.json',p/'analysis.json'),(b/'audit-v2.json',p/'audit.json'),(b/'frozen-plan-v2.json',p/'frozen-plan.json'),(r/'docs/MTP_SUBSET_RESUME_2026-10-23.md',p/'decision.md')]:shutil.copy2(src,dest)
old=json.loads((p/'previous/q4t.release.json').read_text());shutil.copy2(old['build_cache'],p/'previous/CMakeCache.txt')
for f in (b/'artifact').iterdir():shutil.copy2(f,p/'artifact'/f.name)
shutil.copytree(b/'source-snapshot-v2',p/'tested-source')
fixture=b/'fixtures/quality'
(p/'health-fixture/requests.jsonl').write_text((fixture/'requests.jsonl').read_text().splitlines()[0]+'\n')
save(p/'health-fixture/manifest.json',json.loads((fixture/'manifest.json').read_text())[:1])
commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=r,text=True).strip()
metadata=dict(version='mtp-subset-bf16-20261023',binary_sha256=sha(p/'candidate/q4t'),source_commit=commit,source_note='production inputs unchanged from tested frozen snapshot; see frozen-plan and audit',build_cache=str(p/'candidate/CMakeCache.txt'),build_cache_sha256=sha(p/'candidate/CMakeCache.txt'),acceptance='ENGINEERING_ACCEPTED_BOUNDED_OPT_IN',acceptance_snapshot=str(p/'decision.md'),acceptance_snapshot_sha256=sha(p/'decision.md'),acceptance_audit=str(p/'audit.json'),acceptance_audit_sha256=sha(p/'audit.json'),previous_binary=str(p/'previous/q4t'),previous_release=str(p/'previous/q4t.release.json'),scope='default executable; subset head explicit --mtp-subset-head only; full head remains fallback/default; no persistent service',subset_artifact=str(p/'artifact'),subset_manifest_sha256=sha(p/'artifact/manifest.json'),atomicity='individual atomic replaces; not cross-file atomic; recovery marker and identity-checked rollback')
save(p/'candidate/q4t.release.json',metadata)
commands=[]
for name,flags in [('subset',['--mtp','--mtp-subset-head',str(p/'artifact')]),('full',['--mtp']),('plain',[])]:
 commands.append(dict(name=name,command=['python3','-B',str(r/'tools/evalscope/run_acceptance.py'),'--mode','quality','--output',str(r/'.q4t-work/deployment-v2'/name),'--binary',str(root/'build/q4t'),'--model-dir','/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream','--fixtures',str(p/'health-fixture'),'--startup-timeout','300','--port','8753','--max-len','208896',*flags]))
save(p/'health-plan.json',dict(maximum_starts=3,maximum_requests=3,timeout_seconds=1200,purpose='deployment health only; no new performance samples',steps=commands))
save(p/'sealed-files.json',{str(f.relative_to(p)):sha(f) for f in sorted(p.rglob('*')) if f.is_file()})
print(p)

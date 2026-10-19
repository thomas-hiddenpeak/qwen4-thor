import hashlib,json,sys
from pathlib import Path
base=Path('.q4t-work/moe-replay-real-20261019')
run=base/'gpu-v1'
sys.path.insert(0,str(run/'source/tools/verify/moe_replay_real'))
from bundle import validate_bundle,markdown
hash_file=lambda p:hashlib.file_digest(p.open('rb'),'sha256').hexdigest()
files=json.loads((run/'files.json').read_text())
for name,digest in files.items():
 assert hash_file(run/name)==digest,name
state=json.loads((run/'run.json').read_text())
lease=json.loads((base/'lease-v1/exit.json').read_text())
assert state['status']=='PASS' and lease['status']=='COMPLETED' and lease['child_returncode']==0
model=json.loads((run/'model-files.json').read_text())
identity={key:state[key+'_sha256'] for key in ['model','weights','binary','source']}
identity['tokenizer']=next(r['sha256'] for r in model if r['path']=='tokenizer.json')
reports=validate_bundle(run/'capture',identity)
assert reports==json.loads((run/'comparisons.json').read_text())
assert markdown(reports)==(run/'report.md').read_text()
assert all(not e.get('conflicts') for e in map(json.loads,(base/'lease-v1/events.jsonl').read_text().splitlines()))
result=dict(status='PASS',files_verified=len(files),boundary_records=24,comparison_groups=16,same_shape_bitexact=5,independent_prefill_bitexact=True,model_files_hashed=len(model),model_bytes_hashed=sum(r['bytes'] for r in model),guard=lease,identity=identity,raw_root=str(run),run_seconds=state['finished']-state['started'],http_requests=0,performance_claim=False)
(base/'offline-audit.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result,indent=2))

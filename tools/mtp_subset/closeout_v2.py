from pathlib import Path
import json,hashlib,shutil
import argparse
parser=argparse.ArgumentParser()
parser.add_argument('--base',type=Path,required=True)
b=parser.parse_args().base.resolve();r=b/'source';root=b.parents[1];d=r/'.q4t-work/deployment-v2';p=b/'delivery-package-v2';e=r/'docs/evidence/mtp-subset-resume-20261023'
def sha(p):
 with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
health=json.loads((d/'deployment.json').read_text());guard=json.loads((d/'lease/exit.json').read_text())
assert health['status']=='DEPLOYED_HEALTH_CHECKED' and len(health['health'])==3
assert guard['status']=='COMPLETED' and guard['child_returncode']==0 and 'cleanup_error' not in guard
queries=[json.loads(l) for l in (d/'lease/nvml-queries.jsonl').read_text().splitlines()];assert all(q['status']=='OK' for q in queries)
for name,h in json.loads((p/'sealed-files.json').read_text()).items():assert sha(p/name)==h,name
for name in ['q4t','q4t.release.json']:assert sha(root/'build'/name)==sha(p/'candidate'/name)
assert not (root/'build/mtp-subset-recovery.json').exists()
plan=json.loads((b/'frozen-plan-v2.json').read_text())
for name,h in plan['files'].items():
 path=Path(name)
 if path.is_relative_to(r):path=b/'source-snapshot-v2'/path.relative_to(r)
 assert sha(path)==h,str(path)
for name in ['src','include']:
 for path in (b/'source-snapshot-v2'/name).rglob('*'):
  if path.is_file():assert sha(path)==sha(r/path.relative_to(b/'source-snapshot-v2'))
owned=[]
for directory in [r/'.q4t-work/acceptance-v2'/x for x in ['quality-on','quality-off','parent-off','candidate-off','candidate-on','parent-on']]+[d/x for x in ['subset','full','plain']]:
 pid=int((directory/'server.pid').read_text());proc=Path('/proc')/str(pid)
 if proc.exists() and (proc/'comm').read_text().strip()=='q4t':owned.append(pid)
assert not owned
paths=set()
for directory in [b/'source-snapshot-v2',b/'lease-v2',r/'.q4t-work/acceptance-v2',d,p,b/'artifact',b/'fixtures',b/'parent']:
 paths.update(x for x in directory.rglob('*') if x.is_file())
for name in ['frozen-plan-v2.json','analysis-v2.json','memory-v2.json','audit-v2.json','decision-v2.json','build-resume.log','configure-resume.log','deployment-driver.log','deployment-driver-v2.log']:paths.add(b/name)
seal={str(x.relative_to(b)):sha(x) for x in sorted(paths)}
(b/'sealed-files-v2.json').write_text(json.dumps(seal,indent=2)+'\n')
for name,h in seal.items():assert sha(b/name)==h
result=dict(status='DELIVERED_BOUNDED_OPT_IN',candidate_sha256=sha(root/'build/q4t'),source_commit=json.loads((root/'build/q4t.release.json').read_text())['source_commit'],health_requests=3,health_model_starts=3,health_nvml_queries=len(queries),health_nvml_max_seconds=max(q['seconds'] for q in queries),health_guard=guard,retained_preflight_failures=1,preflight_model_starts=0,preflight_http_requests=0,total_http_requests=85,base_http_requests=82,all_owned_servers_gone=True,recovery_marker_cleared=True,default_subset_enabled=False,rollback_package=str(p/'previous'),package_manifest_sha256=sha(p/'sealed-files.json'),final_seal_sha256=sha(b/'sealed-files-v2.json'),final_seal_files=len(seal),tested_snapshot_files_verified=len(plan['files']),runtime_sources_unchanged_since_test=True,branch_retained='codex/mtp-subset-head has unique implementation/evidence commits; no main merge or branch removal',root_source_commit='ab762aa; deployed binary source recorded separately')
(b/'closeout-v2.json').write_text(json.dumps(result,indent=2)+'\n')
for src,name in [(b/'closeout-v2.json','closeout.json'),(d/'deployment.json','deployment.json'),(d/'lease/exit.json','deployment-guard.json'),(root/'build/q4t.release.json','installed.release.json')]:shutil.copy2(src,e/name)
print(json.dumps(result,indent=2))

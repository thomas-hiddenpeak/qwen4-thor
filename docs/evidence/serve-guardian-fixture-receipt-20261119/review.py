from pathlib import Path
import hashlib,json,subprocess,sys,time
root=Path.cwd();out=root/'.q4t-work/serve-realtime-20261119/ci-host-contracts-02'
assert json.loads((out/'result.json').read_text())['status']=='PASS'
tracked=set(subprocess.check_output(['git','ls-files','-z']).decode().split('\0'))-{''}
dirty=set(subprocess.check_output(['git','diff','--name-only','-z']).decode().split('\0'))-{''}
keep={'tools/serve_recovery/fixture_model.py','tools/serve_recovery/test_runtime.py','tools/serve_audit/inventory.json'}
head_files=dirty-keep
branches={''}
for name in head_files | {'src/server/x','include/q4t/server/x','tools/serve_recovery/x','tools/serve_audit/x'}:
 branches.update(str(p) for p in Path(name).parents if str(p)!='.')
for name in tracked:
 if name.startswith(('src/server/','include/q4t/server/')):
  branches.update(str(p) for p in Path(name).parents if str(p)!='.')
view=out/'committed-view'
def mirror(prefix):
 (view/prefix).mkdir();stem=prefix+'/' if prefix else ''
 for child in {p[len(stem):].split('/')[0] for p in tracked if p.startswith(stem)}:
  name=stem+child
  if name in branches: mirror(name)
  elif name in head_files:
   (view/name).write_bytes(subprocess.check_output(['git','show','HEAD:'+name]))
  else: (view/name).symlink_to(root/name)
mirror('')
sys.path.insert(0,str(root))
from tools.serve_audit import check_inventory as check
check.ROOT=view
path=root/'tools/serve_audit/inventory.json';assert path.stat().st_nlink==1
inventory=json.loads(path.read_text());started=time.monotonic();actual=check.current_surface(view)
changes=sorted(n for n in actual['sources_sha256'].keys() | inventory['sources_sha256'].keys() if actual['sources_sha256'].get(n)!=inventory['sources_sha256'].get(n))
assert changes==['tools/serve_recovery/fixture_model.py'],changes
inventory['sources_sha256'][changes[0]]=actual['sources_sha256'][changes[0]]
inventory['fixture_receipt_review_20261119']=dict(source=changes[0],scope='CPU-only actor publishes complete capacity reply by temporary sibling and atomic replace; interruption never exposes partial final JSON. Invalid binding still rejected and recovery budget charged; production code/binary unchanged.',evidence='docs/evidence/serve-guardian-fixture-receipt-20261119/README.md',original_ci_failure_retained=True)
errors=check.check(inventory,actual,view);mutations=check.mutation_checks(inventory,actual)
result=dict(status='PASS' if not errors else 'FAIL',errors=errors,mutation_checks=mutations,source_changes=changes,source_sha256=actual['sources_sha256'][changes[0]],seconds=time.monotonic()-started,gpu_actions=0,semantic_drafts_excluded=True)
(out/'inventory-result.json').write_text(json.dumps(result,indent=2)+'\n')
assert not errors
path.write_text(json.dumps(inventory,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(result))

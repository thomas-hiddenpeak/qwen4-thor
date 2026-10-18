import hashlib,json,subprocess
from pathlib import Path
root=Path('/home/rm01/models/dev/qwen4-thor'); out=Path(__file__).resolve().parent
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
old=json.loads((root/'docs/evidence/mtp-position-b1a-20261017/raw-manifest.json').read_text())
checks=[]
for row in old:
    p=root/row['path']; checks.append({'path':row['path'],'ok':p.is_file() and p.stat().st_size==row['bytes'] and sha(p)==row['sha256']})
assert all(r['ok'] for r in checks)
(out/'reuse-audit.json').write_text(json.dumps(checks,indent=2)+'\n')
for log in ['build.log','build-final.log','build-reference.log']:
    s=(out/log).read_text(); assert 'warning:' not in s and 'error:' not in s,log
prior=root/'.q4t-work/mtp-numerical-closure-20261016/http'
files=[out/x for x in ['build/q4t','q4t-baseline','build/q4t_tests','build/q4t_mtp_lifecycle','reference/q4t_tests','reference/mtp.cu','run_phase.py','build_reference.py','candidate.required','parent.required','build/CMakeCache.txt']]
files += [root/x for x in ['include/q4t/mtp/mtp.h','src/mtp/mtp.cu','src/runtime/memory_budget.cpp','tests/mtp_spec_multi_test.cpp','tests/mtp_lifecycle_test.cpp','tests/mtp_position_metadata_test.cpp','tests/runtime_memory_budget_test.cpp','docs/MTP_GATHER_B1B_2026-10-19.md']]
files += list((root/'tools/evalscope').glob('*.py'))
files += [p for p in prior.rglob('*') if p.is_file() and p.name in ['results.json','requests.jsonl','responses.json']]
model=Path('/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream')
files += [model/'config.json',model/'model.safetensors.index.json']
assert sha(out/'q4t-baseline')=='44abd8b1a7edaca41620b87b4cf894427f8397c1e3529ef56512940ef1b44e03'
identity={'base_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip(),'files':{str(p):sha(p) for p in files},'parent_reference':'Exact parent mtp.cu compiled against additive candidate header; all other inference objects shared. Only fixture pointer-exists assertion disabled for parent, unchanged data comparisons. No performance inference from test executable.','http_requests':82,'timing_resolution':'not established'}
(out/'identity.json').write_text(json.dumps(identity,indent=2)+'\n')
(out/'candidate.patch').write_bytes(subprocess.check_output(['git','diff'],cwd=root))
print('frozen',len(files),'identities; reused',len(checks),'files verified')

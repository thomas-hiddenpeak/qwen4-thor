"""Preserve 28 completed HTTPs; resume only bounded unexecuted F work."""
import json
from pathlib import Path
import shutil
import sys
import time
from prepare import BASE, ROOT, sha, save
from run_stage import validate

old_path=BASE/'feasibility-v2/frozen-plan.json'
old=json.loads(old_path.read_text());first=json.loads((BASE/'feasibility/run/state.json').read_text())
prior=Path(old['output']);state=json.loads((prior/'state.json').read_text())
build=json.loads((BASE/'build-4-result.json').read_text())
if not build['passed'] or not build['inference_unchanged'] or state['requests_completed']!=28:
    raise RuntimeError('resume preconditions')
if not json.loads((prior/'01-full-on/multi-audit.json').read_text())['passed']:
    raise RuntimeError('first service not independently audited')
if json.loads((old_path.parent/'postflight.json').read_text())['final_monitor_errors']:
    raise RuntimeError('old monitor failed')
stage=BASE/'feasibility-v3';stage.mkdir()
programs=stage/'programs';programs.mkdir()
shutil.copy2(build['q4t_tests']['path'],programs/'q4t_tests')
plan=dict(old,output=str(stage/'run'),lease=str(stage/'lease'),http_requests=108,http_loads=3,
    stage_timeout_seconds=int(first['started']+7200-time.time()),
    first_http_reuse=str(prior/'first-http-gate.json'),
    first_http_reuse_sha256=sha(prior/'first-http-gate.json'),
    reused_candidate_sha256=old['candidate_sha256'],
    reused_service=dict(name='01-full-on',path=str(prior/'01-full-on'),requests=28),
    reference_repair=str(ROOT/'docs/MTP_SUFFIX_MULTI_REFERENCE_2026-10-27.md'),
    tests_sha256=sha(programs/'q4t_tests'),steps=[],files={})
for head in ['full','subset']:
    plan['steps'].append(dict(name='contract-'+head,kind='contract',http_loads=0,
        http_requests=0,timeout=450,command=[str(programs/'q4t_tests'),'mtp_suffix_multi_states'],
        env={} if head=='full' else {'Q4T_TEST_SUBSET_HEAD':plan['artifact']}))
for s in old['steps']:
    if s['kind']!='block' or s['name']=='01-full-on':continue
    step=dict(s)
    step['command']=[sys.executable,'-B',str(ROOT/'tools/mtp_suffix_multi/run_stage.py'),
        '--plan',str(stage/'frozen-plan.json'),'--custom-step',step['name']]
    plan['steps'].append(step)
paths={Path(p) for p in old['files']}
paths.update(p for p in prior.rglob('*') if p.is_file())
paths.update([old_path,old_path.parent/'postflight.json',Path(plan['reference_repair']),
              BASE/'build-4-result.json',BASE/'build-4-source.json',BASE/'build-4.log',
              programs/'q4t_tests',Path(build['q4t_tests']['path'])])
paths.update((ROOT/'tools/mtp_suffix_multi').glob('*.py'))
snapshot=stage/'source';snapshot.mkdir()
for p in sorted(paths):
    plan['files'][str(p)]=sha(p)
    if p.is_relative_to(ROOT) and not any(part in p.relative_to(ROOT).parts for part in
                                        ['.q4t-work','.venv','build']):
        dest=snapshot/p.relative_to(ROOT);dest.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(p,dest)
validate(plan);save(stage/'frozen-plan.json',plan)
print(json.dumps(dict(plan=str(stage/'frozen-plan.json'),sha256=sha(stage/'frozen-plan.json'),
    http=108,starts=3,reused_http=28,remaining_seconds=plan['stage_timeout_seconds'])))

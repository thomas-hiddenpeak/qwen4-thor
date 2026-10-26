"""One fixture-only bounded repair; keep the inference SHA and absolute deadline."""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time
from run_stage import ROOT, validate


def sha(p):
    with Path(p).open('rb') as f: return hashlib.file_digest(f,'sha256').hexdigest()

base=ROOT/'.q4t-work/suffix-agent-20261027'
old=base/'acceptance/frozen-plan.json'
plan=json.loads(old.read_text())
state=json.loads((Path(plan['output'])/'state.json').read_text())
build=json.loads((base/'build-4-result.json').read_text())
assert state['completed']==['00-first-http'] and state['requests_completed']==11
assert state['current']=='01-attention-layout' and state['status']=='STOPPED'
assert build['passed'] and build['q4t']['sha256']==plan['candidate_sha256']
allowed={'tests/agent_attention_layout_test.cpp','tools/mtp_suffix_agent/run_stage.py'}
changed=[]
for p,h in plan['files'].items():
    actual=sha(p)
    if actual!=h:
        rel=str(Path(p).relative_to(ROOT))
        assert rel in allowed, ('unexpected change',rel)
        saved=base/'repair-1/prior-source'/rel
        assert sha(saved)==h
        changed.append(rel)
        plan['files'][p]=actual
assert set(changed)==allowed
stage=base/'acceptance-resume-2';stage.mkdir()
tests=stage/'q4t_tests';shutil.copy2(build['q4t_tests']['path'],tests)
plan['prior_plan']=str(old);plan['prior_state']=str(Path(plan['output'])/'state.json')
gate=Path(plan['output'])/'first-http-gate.json'
plan['first_http_reuse']=str(gate);plan['first_http_reuse_sha256']=sha(gate)
plan['prior_requests_completed']=11;plan['prior_starts']=1;plan['prior_contract_attempts']=1
plan['repair_used']=2;plan['remaining_contract_attempts']=3
plan['prior_zero_work_abort']=str(base/'acceptance-resume/run/state.json')
plan['http_requests']=309;plan['http_loads']=14
plan['output']=str(stage/'run');plan['lease']=str(stage/'lease')
plan['stage_timeout_seconds']=int(plan['task_deadline_unix']-time.time())
plan['steps']=plan['steps'][1:]
for s in plan['steps']:
    if s['kind']=='contract':s['command'][0]=str(tests)
    else:s['command']=[sys.executable,'-B',str(ROOT/'tools/mtp_suffix_agent/run_stage.py'),
                       '--plan',str(stage/'frozen-plan.json'),'--custom-step',s['name']]
# Keep every old bound artifact; add repair receipts/source and the unchanged
# first HTTP proof. No fixture, model option, or request order is changed.
for p in [old,gate,Path(plan['prior_state']),tests,*base.glob('build-4*'),
          base/'acceptance/postflight.json',base/'acceptance/run/telemetry-summary.json',
          base/'acceptance/analysis-definition-before-performance.json',
          *ROOT.joinpath('tools/mtp_suffix_agent').glob('*.py'),
          *base.joinpath('repair-1').rglob('*'),
          base/'acceptance-resume/frozen-plan.json',
          base/'acceptance-resume/run/state.json',
          base/'acceptance-resume/postflight.json',
          base/'repair-2/prior-prepare_resume.py',
          base/'repair-2/diagnosis.json']:
    if p.is_file():plan['files'][str(p.resolve())]=sha(p)
validate(plan)
with (stage/'frozen-plan.json').open('x') as f:json.dump(plan,f,indent=2);f.write('\n')
print(json.dumps({'plan':str(stage/'frozen-plan.json'),'sha256':sha(stage/'frozen-plan.json'),
                  'requests':309,'starts':14,'contracts':3,'remaining_seconds':plan['stage_timeout_seconds']}))

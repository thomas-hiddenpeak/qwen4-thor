"""Exact one-record offline argv-path review; original failure and guard remain intact."""
from pathlib import Path
import copy,hashlib,json,os,runpy,shutil,sys,time
R=Path(__file__).resolve().parents[2];W=Path(__file__).parent;O=W/'retained-business-02';OUT=W/'retained-command-review-01'
FILES={'plan.json':'392048a64f4d80e20e51e7b1cc23a6b276ce885ee5e51a2189f085d0a57bd886','lease/exit.json':'2c80e81d6919d636c5a236c2f99af0645374906bae7f9fbc267c6298056e03b1','execution/result.json':'7dc5e73adeca039569a988c55d017b6713a1209168b622fa5f9cae7aa7ff5f3e'}
sha=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest()
read=lambda p:json.loads(Path(p).read_text())
def save(p,v):p.write_text(json.dumps(v,indent=2)+'\n')
assert all(sha(O/n)==h for n,h in FILES.items())
assert Path.cwd()==R and (OUT/'review.json').is_file()
plan=read(O/'plan.json');guard=read(O/'lease/exit.json')
expected=['python3','-I','-B','tools/serve_realtime/speech_service_acceptance.py','run','--plan','.q4t-work/serve-realtime-audio-20261120/retained-business-02/plan.json']
python=read(W/'retained-01/bundle/engine/manifest.json')['python']
def bound(argv):
 assert argv==expected,'only this exact original argv is reviewed'
 assert Path(shutil.which(argv[0])).resolve()==Path(plan['command'][0]).resolve()==Path(python['path'])
 assert sha(python['path'])==python['sha256']
 assert (R/argv[3]).resolve()==Path(plan['command'][3]) and (R/argv[6]).resolve()==Path(plan['command'][6])
 assert sha(R/argv[3])==plan['sources'][argv[3]]
 return list(plan['command'])
proof=read(OUT/'review.json');assert proof['negative_cpu_cases']==7 and proof['files']==FILES
entry=runpy.run_path(str(R/'tools/serve_realtime/speech_service_acceptance.py'));g=entry['GLOBALS'];original=g['accepted_run']
def reviewed(p,result,observed,queries,events):
 assert p==plan and observed==guard and all(sha(O/n)==h for n,h in FILES.items())
 canonical=copy.deepcopy(observed);canonical['command']=bound(observed['command'])
 environment=original(p,result,canonical,queries,events)
 environment['command_identity_review']=dict(path=str(OUT/'review.json'),sha256=sha(OUT/'review.json'),original_exact_argv_passed=False,resolved_files_identical=True)
 return environment
g['accepted_run']=reviewed
g['finalize'](O/'plan.json')
assert all(sha(O/n)==h for n,h in FILES.items())
save(OUT/'result.json',dict(status='PASS_ORIGINAL_BUSINESS_OFFLINE_REVIEW',original_files_unchanged=True,new_gpu_starts=0,decision_sha256=sha(O/'decision.json'),release_sha256=sha(O/'service-release.json')))

"""Reseal the gateway capacity fix; retain the failed run and cumulative budget."""
import json
import os
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from tools.serve_lifecycle import native_acceptance as a
from tools.serve_recovery import bundle
from tools.serve_service import package as p

BASE=ROOT/'.q4t-work/serve-lifecycle-20261109'
OLD=BASE/'business-01';OUT=BASE/'business-02'


def main():
    os.umask(0o077)
    p.require(not OUT.exists(),'N07 completion output exists')
    p.require(not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT),
              'commit the reviewed capacity-binding fix first')
    p.require(p.read(BASE/'gateway-capacity-cpu-01/result.json')['status']=='PASS',
              'N07 admission capacity CPU gate')
    prior=p.read(OLD/'plan.json');result=p.read(OLD/'execution/result.json')
    events=[json.loads(s) for s in (OLD/'lease/events.jsonl').read_text().splitlines()]
    elapsed=next(e['monotonic'] for e in events if e['event']=='lease_released')-next(
        e['monotonic'] for e in events if e['event']=='lease_acquired')
    queries=[json.loads(s) for s in (OLD/'lease/nvml-queries.jsonl').read_text().splitlines()]
    p.require(queries and all(v['status']=='OK' for v in queries),'N07 failed stage had environmental loss')
    changed={n for n,h in prior['sources'].items() if p.sha(ROOT/n)!=h}
    p.require(changed=={'tools/serve_recovery/bundle.py','tools/serve_recovery/gateway.py',
        'tools/serve_recovery/gateway_config.py','tools/serve_lifecycle/native_acceptance.py',
        'tools/serve_lifecycle/test_native_acceptance.py'},
        'N07 completion changed unrelated tested sources')
    p.require(all(p.sha(row['path'])==row['sha256'] for row in prior['protected']),
              'N07 defaults changed')
    OUT.mkdir(mode=0o700);(OUT/'objects').mkdir(mode=0o700)
    expected=prior['validation_release'];p.accepted_release=a.validation_gate(p.accepted_release,expected)
    package=Path(prior['package']['package']);manifest=bundle.verify(package,prior['package']['manifest_sha256'])
    core=p.verify(package/'engine',manifest['engine_manifest_sha256'])
    receipt=bundle.prepare(OLD/'candidate/q4t',OLD/'candidate/q4t.release.json',core['model_dir'],
        package/'bin/q4t-guardian',package/'bin/q4t-cuda-probe',None,OLD/'gateway-policy.json',
        OLD/'client-policy.json',OUT/'bundle',native_receipt=OLD/'native-build.json',
        native_ingress=package/'bin/q4t-ingress',capacity_config=p.read(OLD/'capacity.json'),
        application_state=dict(directory=str(OUT/'objects')))
    p.save(OUT/'receipt.json',receipt)
    plan=dict(prior,output=str(OUT),package=receipt,limits=a.COMPLETION_LIMITS,
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
        resume_capacity_binding=True,prior_counts=result['budget']['counts'],
        prior_seconds=max(elapsed,result['seconds']),
        prior_execution=str(OLD/'execution/result.json'),prior_guard=str(OLD/'lease/exit.json'),
        prior_stopped=str(OLD/'execution/stopped-ledger.json'),prior_early_stop=str(OLD/'early-stop.json'))
    plan['sources']={n:p.sha(ROOT/n) for n in sorted(set(prior['sources'])|{
        'tools/serve_lifecycle/native_resume.py','tools/serve_recovery/test_gateway_capacity.py'})}
    names=['plan.json','early-stop.json','execution/result.json','execution/counts.json',
        'execution/cli-stop.json','execution/stopped-ledger.json','execution/status.jsonl',
        'execution/chat-01-self-check-response.json','lease/exit.json','lease/events.jsonl','lease/nvml-queries.jsonl']
    plan['prior_evidence']=[dict(path=str(OLD/n),sha256=p.sha(OLD/n)) for n in names]
    a.verify_prior(plan)
    p.save(OUT/'plan.json',plan)
    print(json.dumps(dict(plan=str(OUT/'plan.json'),package=receipt,limits=plan['limits'],
                         charged=plan['prior_counts'],prior_seconds=plan['prior_seconds'])))


if __name__=='__main__':main()

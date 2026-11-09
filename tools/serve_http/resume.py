"""Resume only N05's cleaned SDK-prefix failure; keep all spent work charged."""
import fcntl
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from tools.serve_http import acceptance as a
from tools.serve_service import package as p
BASE=ROOT/'.q4t-work/serve-http-20261109'


def main():
    out=BASE/'business-02';prior_dir=BASE/'business-01'
    p.require(not out.exists(),'resume output exists')
    p.require(not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT),'commit fix before resume')
    p.require(p.read(BASE/'prefix-cpu-01/result.json')['exit_code']==0 and
              p.read(BASE/'prefix-node-01/result.json')['exit_code']==0,'prefix CPU gate')
    plan=p.read(prior_dir/'plan.json');prior=p.read(prior_dir/'execution/result.json')
    changes={'tools/serve_http/acceptance.py','tools/serve_http/test_acceptance.py',
             'tools/examples/chat_client.py','tools/examples/js/chat_client.mjs'}
    p.require(all(n in changes or p.sha(ROOT/n)==h for n,h in plan['sources'].items()),'unrelated frozen source changed')
    for row in plan['protected']:p.require(p.sha(row['path'])==row['sha256'],'default changed')
    for owner in prior['models'].values():
        path=Path('/proc')/str(owner['pid'])/'stat'
        if path.exists():p.require(int(path.read_text().rsplit(')',1)[1].split()[19])!=owner['start_ticks'],'old model alive')
    for port in (18871,18872,18873):
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);sock.bind(('127.0.0.1',port))
    lock=ROOT/'.q4t-work/gpu-exclusive.lock'
    with lock.open('r+') as stream:fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
    plan.update(output=str(out),resume_after_prefix=True,limits=a.COMPLETION_LIMITS,
        prior_counts=prior['budget']['counts'],prior_seconds=prior['seconds'],
        prior_execution=str(prior_dir/'execution/result.json'),prior_guard=str(prior_dir/'lease/exit.json'),
        prior_stopped=str(prior_dir/'execution/stopped-ledger.json'))
    files=[prior_dir/'plan.json',Path(plan['prior_execution']),Path(plan['prior_guard']),Path(plan['prior_stopped']),
           prior_dir/'execution/core-chunked-response.json',prior_dir/'execution/native-json-response.json',
           prior_dir/'execution/native-sse-response.json',prior_dir/'execution/native-reauth-http.json',
           prior_dir/'execution/core-reuse-http.json',prior_dir/'execution/native-reuse-http.json',
           prior_dir/'execution/official-sdk-wire.bin']
    plan['prior_evidence']=[dict(path=str(f),sha256=p.sha(f)) for f in files]
    plan['sources']={n:p.sha(ROOT/n) for n in plan['sources']}
    for n in ('tools/serve_http/resume.py','tools/serve_http/test_client_prefix.py','tools/serve_http/test_client_prefix.mjs'):
        plan['sources'][n]=p.sha(ROOT/n)
    plan['source_commit']=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    plan['reuse']='Prior self-check/core chunked/100, native JSON/SSE, reuse/reauth; same engine/ingress/runtime.'
    a.verify_prior(plan);a.verify(plan)
    out.mkdir(mode=0o700);p.save(out/'plan.json',plan)
    p.save(out/'independent-cleanup-check.json',dict(status='PASS_STOPPED_CLEANUP',
        model_owners_gone=True,ports_released=True,lock_acquirable=True,default_unchanged=True,
        previous_failure_preserved=True,new_gpu_operations=0))
    print(json.dumps(dict(plan=str(out/'plan.json'),limits=plan['limits'],prior_counts=plan['prior_counts'],
                         prior_seconds=plan['prior_seconds'],production_binary_unchanged=True)))


if __name__=='__main__':os.umask(0o077);main()

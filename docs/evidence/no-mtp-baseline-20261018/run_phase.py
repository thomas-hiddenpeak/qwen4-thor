"""Frozen 41-request no-MTP baseline. --check never starts a server."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from check_identity import check, sha

ROOT=Path('/home/rm01/models/dev/qwen4-thor')
OUT=Path(__file__).resolve().parent
PRIOR=ROOT/'.q4t-work/mtp-numerical-closure-20261016/http'
PYTHON=ROOT/'tools/evalscope/.venv/bin/python'
BINARY=ROOT/'.q4t-work/b1a-20261017/q4t-baseline'
SCHEDULE=[('Q','quality',11),('A0','performance',15),('A1','performance',15)]

def save(name,value):
    (OUT/name).write_text(json.dumps(value,indent=2)+'\n')

def commands():
    result=[]
    for name,mode,count in SCHEDULE:
        ref=PRIOR/('quality-off' if mode=='quality' else 'off')
        fixture=ref/'inputs' if mode=='quality' else ref
        cmd=[str(PYTHON),str(ROOT/'tools/evalscope/run_acceptance.py'),
             '--binary',str(BINARY),'--model-dir',
             '/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream',
             '--startup-timeout','300','--max-len','208896',
             '--allow-unqualified-binary','--mode',mode,'--output',str(OUT/name),
             '--reference',str(ref/'results.json'),'--fixtures',str(fixture),'--port','8740']
        result.append({'name':name,'mode':mode,'requests':count,'argv':cmd})
    return result

def audit():
    frozen=json.loads((ROOT/'docs/evidence/no-mtp-baseline-bounded-20261018/plan.json').read_text())
    assert frozen['stage_1_caps']=={'http_generation_requests':41,'server_starts':3,'wall_seconds':14400,'retries':0}
    cmds=commands()
    assert sum(c['requests'] for c in cmds)==41 and len(cmds)==3
    manifest=json.loads((PRIOR/'quality-off/inputs/manifest.json').read_text())
    assert len(manifest)==11
    assert len((PRIOR/'quality-off/inputs/requests.jsonl').read_text().splitlines())==11
    for n in frozen['performance']['lengths']:
        lines=(PRIOR/f'off/context-{n}/requests.jsonl').read_text().splitlines()
        assert len(lines)==3 and len(set(lines))==1
    forbidden={'--mtp','--mtp-default','--mtp-k','--perf-lengths','--extra-lengths','--target-total'}
    assert not any(forbidden.intersection(c['argv']) for c in cmds)
    return {'status':'PASS','commands':cmds,'reserved_requests':41,'server_starts':3,
            'client_protocol':'unchanged batched default; runner no MTP flag means server --no-mtp',
            'no_retries':True,'wrapper_timeout_seconds':14200,'kill_after_seconds':30,
            'lease_cleanup_reserve_seconds':170,
            'driver_sha256':sha(Path(__file__)),'identity_checker_sha256':sha(OUT/'check_identity.py')}

def snapshot(name):
    value=check(); save(name,value)
    if value['status']!='PASS': raise RuntimeError('IDENTITY_MISMATCH: '+str(value['mismatches']))
    if value['model_metadata']!=json.loads((OUT/'preflight.json').read_text())['model_metadata']:
        raise RuntimeError('IDENTITY_MISMATCH: model metadata changed')

def main():
    args=argparse.ArgumentParser(); args.add_argument('--check',action='store_true'); args=args.parse_args()
    result=audit()
    if args.check:
        save('driver-audit.json',result); print('PASS: 11+15+15=41; 3 starts; no retries; default five-tier client'); return
    assert not (OUT/'phase-exit.json').exists()
    for name,_,_ in SCHEDULE: assert not (OUT/name).exists()
    assert sha(Path(__file__))==json.loads((OUT/'driver-audit.json').read_text())['driver_sha256']
    env={k:v for k,v in os.environ.items() if not k.startswith('Q4T_') and k!='LD_PRELOAD'}
    env['CUDA_HOME']='/usr/local/cuda-13.3'; env['LD_LIBRARY_PATH']='/usr/local/cuda-13.3/lib64'
    save('execution-plan.json',{'authorization':'user authorized frozen 41-request plan; B1a paused',**result})
    save('environment.json',{'Q4T_variables':{k:v for k,v in env.items() if k.startswith('Q4T_')},'CUDA_HOME':env['CUDA_HOME'],'LD_LIBRARY_PATH':env['LD_LIBRARY_PATH'],'LD_PRELOAD':env.get('LD_PRELOAD')})
    state={'status':'RUNNING','reserved_requests':0,'server_starts':0,'completed':[], 'start_monotonic':time.monotonic()}
    current=None
    def interrupted(signum,frame): raise KeyboardInterrupt(f'signal {signum}')
    signal.signal(signal.SIGINT,interrupted); signal.signal(signal.SIGTERM,interrupted)
    sys.path.insert(0,str(ROOT/'tools/evalscope'))
    from gpu_exclusive import stop_owned
    try:
        for c in result['commands']:
            name=c['name']; snapshot(name+'-before-identity.json')
            state['reserved_requests']+=c['requests']; state['server_starts']+=1
            assert state['reserved_requests']<=41 and state['server_starts']<=3
            save('ledger.json',state)
            with (OUT/(name+'-driver.log')).open('w') as log:
                current=subprocess.Popen(c['argv'],cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                rc=current.wait()
            save(name+'-driver-exit.json',{'returncode':rc,'argv':c['argv'],'pid':current.pid})
            stop_owned(current,15)  # Verify own process group/descendants drained.
            current=None
            if rc: raise RuntimeError(f'QUALITY_OR_PROTOCOL_FAIL: {name} runner rc {rc}')
            exit_record=json.loads((OUT/name/'exit.json').read_text())
            assert exit_record['http_output_checks_passed'] and exit_record['server']==0
            server_pid=int((OUT/name/'server.pid').read_text())
            assert not Path(f'/proc/{server_pid}').exists(), 'server still present'
            if c['mode']=='quality':
                rows=json.loads((OUT/name/'quality/responses.json').read_text())
                assert len(rows)==11
            else:
                rows=[]
                for n in [1024,4096,8192,45056,204800]:
                    batch=json.loads((OUT/name/f'context-{n}/responses.json').read_text())
                    old=json.loads((PRIOR/f'off/context-{n}/responses.json').read_text())
                    assert len(batch)==3
                    assert all(r['actual_input']==old[j]['actual_input'] and r['actual_output']==256 for j,r in enumerate(batch))
                    rows.extend(batch)
                assert len(rows)==15
            assert len(rows)==c['requests'] and all(r['response_id_valid'] for r in rows)
            save(name+'-request-audit.json',{'requests':len(rows),'response_identity_valid':True,'actual_lengths_verified':True})
            snapshot(name+'-after-identity.json')
            state['completed'].append(name); save('ledger.json',state)
            print(name+' completed; lease remains held',flush=True)
            if name!='A1':
                begin=time.monotonic(); time.sleep(60)
                save(name+'-restart-gap.json',{'begin_monotonic':begin,'end_monotonic':time.monotonic(),'minimum_seconds':60})
        state['status']='BASELINE_RECORDED_DESCRIPTIVE'
    except BaseException as error:
        state['status']='INTERRUPTED' if isinstance(error,KeyboardInterrupt) else 'STOPPED'
        state['reason']=repr(error)
        raise
    finally:
        try:
            if current is not None: stop_owned(current,15)
        finally:
            state['end_monotonic']=time.monotonic(); save('phase-exit.json',state)

if __name__=='__main__': main()

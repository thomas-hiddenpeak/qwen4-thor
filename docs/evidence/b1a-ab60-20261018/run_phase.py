"""Frozen 60-request B1a A/B observation. --check never starts a server."""
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
PLAN=json.loads((ROOT/'docs/evidence/mtp-position-b1a-ab60-plan-20261018/plan.json').read_text())
SCHEDULE=[(c['service'],'performance',c['requests']) for c in PLAN['schedule']]

def save(name,value):
    (OUT/name).write_text(json.dumps(value,indent=2)+'\n')

def commands():
    return [{'name':c['service'],'mode':'performance','mtp':c['mode']=='on','requests':c['requests'],
             'argv':[x.replace('{RUN_ROOT}',str(OUT)) for x in c['argv_template']]} for c in PLAN['schedule']]

def audit():
    cmds=commands()
    assert PLAN['planned_caps']=={'requests':60,'server_starts':4,'wall_seconds':14400,'retries':0}
    assert [c['name'] for c in cmds]==['A-off','B-off','B-on','A-on']
    assert sum(c['requests'] for c in cmds)==60 and len(cmds)==4
    for mode in ['off','on']:
        for n,actual in zip(PLAN['nominal_lengths'],PLAN['actual_lengths']):
            ref=PRIOR/mode/f'context-{n}'
            lines=(ref/'requests.jsonl').read_text().splitlines()
            rows=json.loads((ref/'responses.json').read_text())
            assert len(lines)==3 and len(set(lines))==1
            assert len(rows)==3 and all(r['actual_input']==actual for r in rows)
    assert all('--perf-lengths' not in c['argv'] for c in cmds)
    assert [('--mtp-default' in c['argv']) for c in cmds]==[False,False,True,True]
    return {'status':'PASS','commands':cmds,'reserved_requests':60,'server_starts':4,
            'client_protocol':'unchanged batched default','no_retries':True,
            'wrapper_timeout_seconds':14200,'kill_after_seconds':30,
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
        save('driver-audit.json',result); print('PASS: 4x15=60; A-off/B-off/B-on/A-on; no retries'); return
    assert not (OUT/'phase-exit.json').exists()
    for name,_,_ in SCHEDULE: assert not (OUT/name).exists()
    assert sha(Path(__file__))==json.loads((OUT/'driver-audit.json').read_text())['driver_sha256']
    env={k:v for k,v in os.environ.items() if not k.startswith('Q4T_') and k!='LD_PRELOAD'}
    env['CUDA_HOME']='/usr/local/cuda-13.3'; env['LD_LIBRARY_PATH']='/usr/local/cuda-13.3/lib64'
    save('execution-plan.json',{'authorization':'user requested filling missing evidence; execute frozen AB60 only, no extra sampling',**result})
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
            assert state['reserved_requests']<=60 and state['server_starts']<=4
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
                    old=json.loads((PRIOR/('on' if c['mtp'] else 'off')/f'context-{n}/responses.json').read_text())
                    assert len(batch)==3
                    assert all(r['actual_input']==old[j]['actual_input'] and r['actual_output']==256 for j,r in enumerate(batch))
                    rows.extend(batch)
                assert len(rows)==15
            assert len(rows)==c['requests'] and all(r['response_id_valid'] for r in rows)
            server_log=(OUT/name/'server.log').read_text()
            if c['mtp']:
                assert 'MTP loaded (k=3 max_seq=1)' in server_log
                assert 'effective mtp=1' in server_log
            else:
                assert 'effective mtp=0' in server_log
            save(name+'-request-audit.json',{'requests':len(rows),'response_identity_valid':True,'actual_lengths_verified':True})
            snapshot(name+'-after-identity.json')
            state['completed'].append(name); save('ledger.json',state)
            print(name+' completed; lease remains held',flush=True)
            if name!='A-on':
                begin=time.monotonic(); time.sleep(60)
                save(name+'-restart-gap.json',{'begin_monotonic':begin,'end_monotonic':time.monotonic(),'minimum_seconds':60})
        state['status']='AB_OBSERVATIONS_RECORDED'
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

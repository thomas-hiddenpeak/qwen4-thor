"""B1b frozen 82-request HTTP phase followed by bounded direct contracts."""
import os, json, subprocess, signal, sys, time, hashlib
from pathlib import Path
root=Path('/home/rm01/models/dev/qwen4-thor'); out=Path(__file__).resolve().parent
prior=root/'.q4t-work/mtp-numerical-closure-20261016/http'
sys.path.insert(0,str(root/'tools/evalscope'))
from gpu_exclusive import stop_owned

def save(name,value): (out/name).write_text(json.dumps(value,indent=2)+'\n')
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
env={k:v for k,v in os.environ.items() if not k.startswith('Q4T_') and k!='LD_PRELOAD'}
env['CUDA_HOME']='/usr/local/cuda-13.3'; env['LD_LIBRARY_PATH']='/usr/local/cuda-13.3/lib64'
state={'status':'RUNNING','reserved_requests':0,'completed':[],'start':time.time()}; current=None

def run(name,cmd,timeout,extra=None):
    global current
    save(name+'-command.json',cmd)
    with (out/(name+'.log')).open('w') as log:
        current=subprocess.Popen(cmd,cwd=root,env={**env,**(extra or {})},stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        rc=current.wait(timeout=timeout)
    save(name+'-exit.json',{'returncode':rc})
    stop_owned(current,15); current=None
    if rc: raise RuntimeError(name+' failed: '+str(rc))

def stop(signum,frame): raise KeyboardInterrupt(str(signum))
signal.signal(signal.SIGTERM,stop); signal.signal(signal.SIGINT,stop)
try:
    identity=json.loads((out/'identity.json').read_text())
    for f,digest in identity['files'].items(): assert sha(Path(f))==digest,f
    save('environment.json',env)
    schedule=[('quality-on','quality',True,False),('candidate-on','performance',True,False),('quality-off','quality',False,False),('candidate-off','performance',False,False),('parent-off','performance',False,True),('parent-on','performance',True,True)]
    for name,mode,mtp,parent in schedule:
        assert time.time()-state['start']<14400
        n=11 if mode=='quality' else 15
        state['reserved_requests']+=n; assert state['reserved_requests']<=82; save('ledger.json',state)
        ref=('quality-' if mode=='quality' else '')+('on' if mtp else 'off')
        cmd=[str(root/'tools/evalscope/.venv/bin/python3'),str(root/'tools/evalscope/run_acceptance.py'),'--binary',str(out/('q4t-baseline' if parent else 'build/q4t')),'--model-dir','/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream','--startup-timeout','300','--allow-unqualified-binary','--mode',mode,'--output',str(out/name),'--reference',str(prior/ref/'results.json'),'--port','8740']
        if mtp: cmd+=['--mtp-default']
        if mode=='performance': cmd+=['--fixtures',str(prior/ref)]
        run(name+'-driver',cmd,max(1,14400-(time.time()-state['start'])))
        result=json.loads((out/name/'exit.json').read_text()); assert result['http_output_checks_passed'] and result['server']==0
        state['completed'].append(name); save('ledger.json',state); print(name+' PASS; lease held',flush=True)
    (out/'http-complete').write_text('passed\n')
    direct_start=time.time()
    for name,binary,required in [('candidate-contracts',out/'build/q4t_tests',out/'candidate.required'),('parent-contracts',out/'reference/q4t_tests',out/'parent.required')]:
        dump=out/(name+'-state'); dump.mkdir()
        run(name,[str(binary),'--required-list',str(required)],max(1,7200-(time.time()-direct_start)),{'Q4T_MTP_CONTRACT_DIR':str(dump)})
    a=out/'parent-contracts-state'; b=out/'candidate-contracts-state'
    assert sorted(p.name for p in a.iterdir())==sorted(p.name for p in b.iterdir())
    comparison={p.name:{'bytes':p.stat().st_size,'parent':sha(p),'candidate':sha(b/p.name)} for p in a.iterdir()}
    save('state-comparison.json',comparison)
    assert len(comparison)==12 and all(v['parent']==v['candidate'] for v in comparison.values())
    run('lifecycle',[str(out/'build/q4t_mtp_lifecycle'),str(out/'lifecycle-fixture'),'/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream'],max(1,7200-(time.time()-direct_start)))
    state['status']='VALIDATION_COMPLETED_PENDING_REVIEW'
except BaseException as error:
    state['status']='STOPPED'; state['reason']=repr(error); raise
finally:
    if current is not None: stop_owned(current,15)
    state['end']=time.time(); save('phase-exit.json',state)

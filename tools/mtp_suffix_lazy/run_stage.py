"""Conditional B driver draft; never run before A selection and B identity freeze."""
import importlib.util
import json
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('lazy_reused_monitor',ROOT/'tools/mtp_suffix_zero_hit/run_rerun.py')
reuse=importlib.util.module_from_spec(spec);spec.loader.exec_module(reuse)
legacy,admission,http= reuse.legacy,reuse.admission,reuse.http
save=legacy.save

def validate(plan):
    if plan['selection']['status']!='SELECTED_AFTER_VALID_A':raise ValueError('B not selected')
    if time.time()>=plan['task_deadline_unix']:raise TimeoutError('absolute task deadline')
    if (plan['http_requests'],plan['http_loads'],plan['stage_timeout_seconds'])!=(226,10,14400):raise ValueError('B budget')
    if len(plan['steps'])!=10:raise ValueError('B starts')
    if sum(s['http_requests'] for s in plan['steps'])!=226:raise ValueError('B requests')
    if sum(11 for s in plan['steps'] if s['quality'])!=22:raise ValueError('B quality')
    if sum(3 for s in plan['steps'] if s['lifecycle'])!=6:raise ValueError('B lifecycle')
    if not plan['steps'][0]['quality'] or plan['steps'][0]['head']!='full' or not plan['steps'][0]['suffix']:raise ValueError('first fullON quality gate')
    for s in plan['steps']:
        count=sum(c['repeats'] for c in s['cases'])
        if count!=(15 if s['head']=='plain' else 21) or s['http_requests']!=count+11*s['quality']+3*s['lifecycle']:raise ValueError('B service count')


def quality(plan,output,server,env,capacity,check):
    directory=output/'quality';directory.mkdir()
    source=Path(plan['quality_fixtures']);inputs=directory/'requests.jsonl';inputs.write_bytes((source/'requests.jsonl').read_bytes())
    expected={r['prompt_sha256']:r for r in json.loads((source/'manifest.json').read_text())}
    rows=http.run_case(output,directory,inputs,11,1,208896,32,True,ROOT/'tools/evalscope/.venv/bin/evalscope',
                       Path(plan['model']),plan['port'],env,server,False,capacity,'quality',process_check=check)
    for r in rows:
        r.update(expected[r['prompt_sha256']]);r['exact_match']=r['text'].strip()==r['expected'];r['length_match']=r['actual_input']==r['length']
    save(output/'quality-results.json',rows)
    if len({r['id'] for r in rows})!=11 or not all(r['exact_match'] and r['length_match'] and r['finish']==['stop'] for r in rows):raise RuntimeError('quality gate failed')
    raw=(output/'server.log').read_text()
    parse=lambda line:dict(re.findall(r'(\w+)=(\S+)',line))
    admitted=[parse(l)['id'] for l in raw.splitlines() if 'event=admitted ' in l]
    ended=[parse(l) for l in raw.splitlines() if 'event=end ' in l]
    suffix=[parse(l) for l in raw.splitlines() if l.startswith('[suffix] ')]
    if (len(admitted)!=11 or len(ended)!=11 or len(suffix)!=11 or
        set(admitted)!={r['response_id'] for r in rows} or
        set(admitted)!={r['id'] for r in ended} or
        any(r.get('released')!='1' for r in ended)):
        raise RuntimeError('quality release/suffix lifecycle')
    for r in suffix:audit_stats(r)



def contracts(plan,output):
    started=time.monotonic();results=[];failure=None
    try:
        for n,c in enumerate(plan['contracts']):
            with (output/f'contract-{n}.log').open('x') as log:
                result=subprocess.run(c['command'],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,
                    timeout=min(c['timeout'],max(.01,plan['contract_seconds']-(time.monotonic()-started))))
            results.append(dict(name=c['name'],returncode=result.returncode))
            if result.returncode:raise RuntimeError('affected contract failed: '+c['name'])
    except BaseException as e:
        failure=repr(e);raise
    finally:save(output/'contract-results.json',dict(results=results,failure=failure,elapsed_seconds=time.monotonic()-started))


def custom_step(plan,name):
    legacy.require_guard();step=next(s for s in plan['steps'] if s['name']==name)
    out=Path(plan['output'])/name;out.mkdir(exist_ok=False)
    env={k:v for k,v in legacy.clean_env().items() if not k.startswith('CUPTI_')}
    command=[step['binary'],'serve','--model-dir',plan['model'],'--host','127.0.0.1','--port',str(plan['port']),
             '--max-seq','1','--max-prefill','8192','--max-len','208896','--max-tokens','256']
    command+=['--no-mtp'] if step['head']=='plain' else ['--mtp','--mtp-k','3']
    if step['head']=='subset':command+=['--mtp-subset-head',plan['artifact']]
    if step['suffix']:command+=['--mtp-suffix']
    save(out/'server-command.json',dict(argv=command,environment={k:v for k,v in env.items() if k.startswith('Q4T_')}));save(out/'step.json',step)
    passed=False;failure=None;server=None;deadline=time.monotonic()+step['timeout']-45
    def check():
        if time.monotonic()>=deadline or time.time()>=plan['task_deadline_unix']:raise TimeoutError('bounded B step')
        if server is not None and server.poll() is not None:raise RuntimeError('B server exited unexpectedly')
    with (out/'server.log').open('x') as log:
        try:
            http.event(out,'server_before_start');server=subprocess.Popen(command,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT)
            (out/'server.pid').write_text(str(server.pid)+'\n');http.event(out,'server_started',pid=server.pid)
            startup=min(deadline,time.monotonic()+900)
            while time.monotonic()<startup:
                check()
                if 'serving on port' in (out/'server.log').read_text():break
                time.sleep(1)
            else:raise TimeoutError('B startup')
            capacity=http.capacity_evidence((out/'server.log').read_text(),208896);save(out/'capacity.json',capacity)
            http.event(out,'server_ready',pid=server.pid,capacity=capacity)
            if not capacity['matches_requested']:raise RuntimeError('B capacity')
            if step['quality']:
                quality(plan,out,server,env,capacity,check)
                if step==plan['steps'][0]:
                    save(Path(plan['output'])/'first-http-gate.json',dict(passed=True,requests=11,head='full',suffix=True,unix=time.time()))
                    contracts(plan,out)
            results=admission.run_cases(plan,step,out,server,env,capacity,client_check=check)
            for c,r in zip(step['cases'],results,strict=True):
                if r['outputs']!=[c['expected_output_sha256']]*3:raise RuntimeError('B frozen output differs: '+c['id'])
            if step['lifecycle']:save(out/'lifecycle-results.json',admission.lifecycle_admission(plan,step,out))
            check();passed=True
        except BaseException as e:failure=repr(e);raise
        finally:
            if server is not None:
                http.event(out,'server_before_shutdown',pid=server.pid)
                if server.poll() is None:
                    server.terminate()
                    try:server.wait(timeout=30)
                    except subprocess.TimeoutExpired:server.kill();server.wait(timeout=10)
                http.event(out,'server_after_shutdown',pid=server.pid,server_returncode=server.returncode)
            save(out/'exit.json',dict(server=server.returncode if server else None,http_output_checks_passed=passed and server is not None and server.returncode==0,failure=failure))
    if not passed or server.returncode!=0:raise RuntimeError('B service did not finish')


def audit_stats(row):
    keys=['queries','hits','no_match','incomplete','d0_mismatch','invalid','exhausted',
          'numeric_checks','classified_tokens','numeric_rejected','short_rejected',
          'classify_failed','cache_decodes','cache_lookups','cache_allocations',
          'cache_bytes','peak_bytes','numeric_ns','query_ns']
    r={k:int(row[k]) for k in keys}
    if min(r.values())<0 or r['invalid'] or r['exhausted'] or r['classify_failed']:
        raise RuntimeError('invalid/failed suffix or cache')
    if not 0<r['peak_bytes']<=2*1024*1024 or r['cache_bytes'] not in (0,248320):raise RuntimeError('payload bound')
    outcomes=sum(r[k] for k in ['hits','no_match','incomplete','d0_mismatch','numeric_rejected','short_rejected'])
    if (outcomes!=r['queries'] or r['numeric_checks']!=r['hits']+r['numeric_rejected'] or
        not 32*r['numeric_checks']<=r['classified_tokens']<=64*r['numeric_checks'] or
        r['cache_lookups']!=r['classified_tokens'] or r['cache_decodes']>r['cache_lookups'] or
        r['numeric_ns']>r['query_ns'] or r['cache_allocations']>1):raise RuntimeError('lazy admission accounting')
    accepted=[int(x) for x in row['accepted'].split(',')];neural=[int(x) for x in row['neural_accepted'].split(',')]
    if len(accepted)!=4 or len(neural)!=4 or min(accepted+neural)<0 or sum(accepted)!=r['hits'] or sum(accepted+neural)!=r['queries']:raise RuntimeError('acceptance accounting')
    return r


def audit_server(output,expected_requests):
    admission._BASE_AUDIT(output,expected_requests)
    text=(output/'server.log').read_text();step=json.loads((output/'step.json').read_text())
    if '[suffix-causal]' in text or any(s in text for s in ['MTP trunk alloc failed','plain decode for this request','MTP load failed','MTP allocation failed']):raise RuntimeError('wrong path/fallback')
    fields=lambda line:dict(re.findall(r'(\w+)=(\S+)',line))
    admitted={};ended={}
    for n,line in enumerate(text.splitlines()):
        if 'event=admitted ' in line or 'event=end ' in line:
            r=fields(line);target=admitted if r['event']=='admitted' else ended
            if not r.get('id') or r['id'] in target:raise RuntimeError('duplicate lifecycle')
            target[r['id']]=dict(r,line=n)
    if len(admitted)!=expected_requests or admitted.keys()!=ended.keys() or any(r.get('released')!='1' or r['line']<=admitted[k]['line'] for k,r in ended.items()):raise RuntimeError('release lifecycle')
    table=[dict(fields(l),line=n) for n,l in enumerate(text.splitlines()) if l.startswith('[suffix-classes] ')]
    summaries=json.loads((output/'server-audit.json').read_text())['suffix']
    if step['suffix']:
        if len(table)!=2 or [r['event'] for r in table]!=['configure','release']:raise RuntimeError('eager/missing cache lifetime')
        config,end=table
        if (config.get('lazy')!='1' or int(config['bytes'])!=0 or int(config['vocab'])!=248320 or int(config['cap_bytes'])!=262144 or int(config['object_bytes'])>128):raise RuntimeError('lazy startup contract')
        if config['line']>=min(r['line'] for r in admitted.values()) or end['line']<=max(r['line'] for r in ended.values()):raise RuntimeError('cache outside request lifetime')
        if {r['request'] for r in summaries}!=admitted.keys():raise RuntimeError('suffix request identity')
        numeric=[audit_stats(r) for r in summaries]
        if (int(end['bytes']) not in (0,248320) or int(end['peak_bytes'])!=int(end['bytes']) or
            int(end['allocations'])!=bool(int(end['bytes'])) or int(end['unknown'])>min(243,int(end['decodes'])) or
            int(end['checks'])!=sum(r['numeric_checks'] for r in numeric) or
            int(end['lookups'])!=sum(r['cache_lookups'] for r in numeric) or
            int(end['decodes'])!=sum(r['cache_decodes'] for r in numeric) or
            int(end['allocations'])!=sum(r['cache_allocations'] for r in numeric) or
            int(end['decodes'])>248320):raise RuntimeError('global cache accounting')
        for r in summaries:
            if r['request'].endswith('-cancel') and (int(r['hits'])<=0 or ended[r['request']].get('outcome')!='cancelled' or ended[r['request']].get('reason')!='explicit_cancel'):raise RuntimeError('cancel after actual suffix use required')
    elif table:raise RuntimeError('suffix-off cache work')
    hist={fields(l)['request']:[int(x) for x in fields(l)['draft_counts'].split(',')] for l in text.splitlines() if l.startswith('[mtp-accept] ')}
    cases=json.loads((output/'results.json').read_text())
    for c,r in zip(step['cases'],cases,strict=True):
        if c.get('expected_histogram') is not None:
            for response in r['responses']:
                if hist.get(response['response_id'])!=c['expected_histogram']:raise RuntimeError('frozen acceptance/work histogram changed')
    save(output/'lazy-audit.json',dict(passed=True,table=table,requests=summaries,
         accounting='numeric_ns includes cache allocation/lookup/decode; do not add them again',histograms=hist))


if __name__=='__main__':
    def interrupted(*_):raise KeyboardInterrupt('owned B driver terminated')
    signal.signal(signal.SIGTERM,interrupted)
    reuse.validate=validate;reuse.custom_step=custom_step;reuse.admission.audit_server=audit_server
    reuse.main()

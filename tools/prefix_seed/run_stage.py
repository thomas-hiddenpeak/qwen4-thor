"""Explicit seed HTTP validation; unchanged guard/telemetry and cache helpers."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('prefix_previous',ROOT/'tools/prefix_cache/run_stage.py')
old=importlib.util.module_from_spec(spec);spec.loader.exec_module(old)
reuse,legacy,http,multi,save=old.reuse,old.legacy,old.http,old.multi,old.save
request,capability,cached,turn=old.request,old.capability,old.cached,old.turn


def rejected_probes(plan,out):
    probes=[({'prefix_cache_write_tokens':8192},'requires prefix_cache_scope'),
        ({'prefix_cache_scope':capability('bad'),'prefix_cache_write_tokens':True},'must be 0 or an integer'),
        ({'prefix_cache_scope':capability('bad'),'prefix_cache_write_tokens':1024},'not an allowed checkpoint'),
        ({'prefix_cache_scope':capability('bad'),'prefix_cache_write_tokens':8192},'requires an uncached prompt token')]
    results=[]
    for i,(extra,expected) in enumerate(probes):
        body=dict(model='qwen3.8-flash-next',prompt='x',max_tokens=1,request_id='seed-reject-'+str(i),
            cancel_token=capability('seed-reject-cancel-'+str(i)),**extra)
        req=urllib.request.Request(f"http://127.0.0.1:{plan['port']}/v1/chat/completions",
            data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(req,timeout=30) as response:
                raise RuntimeError('invalid seed accepted: '+str(response.status))
        except urllib.error.HTTPError as error:
            text=error.read().decode()
            results.append(dict(request=body,status=error.code,response=text,expected=expected))
            save(out/'rejected-probes.json',results)
            if error.code!=400 or expected not in text:raise RuntimeError('wrong seed rejection')
    save(out/'rejected-probes.json',results)


def lifecycle(plan,out,check):
    prompt=Path(plan['development_prompt']).read_text()
    scope=capability('seed-A-scope');child=capability('seed-A-child')
    question='What is the audit_code in the authoritative shared record? Return only its six digits.'
    cold=request(plan,out,'readonly-cold',prompt,8,{'prefix_cache_scope':scope})
    again=request(plan,out,'readonly-again',prompt+cold['text']+turn(question),16,{'prefix_cache_scope':scope})
    if cached(cold) or cached(again) or again['text'].strip()!='831907':raise RuntimeError('read-only miss wrote or recall')
    source=request(plan,out,'source',prompt,8,{'prefix_cache_scope':scope,'prefix_cache_write_tokens':8192})
    request(plan,out,'overwrite',plan['short_prompt'],16)
    branch=prompt+source['text']+turn(question)
    hit=request(plan,out,'child',branch,16,{'prefix_cache_scope':scope,'session_id':child})
    fresh=request(plan,out,'fresh',branch,16)
    isolated=request(plan,out,'isolated',branch,16,{'prefix_cache_scope':capability('other-scope')})
    mismatch=request(plan,out,'mismatch',branch.replace('archive','record',1),16,{'prefix_cache_scope':scope})
    if cached(source) or source['usage']['completion_tokens']!=8 or cached(hit)!=8192 or cached(isolated) or cached(mismatch):raise RuntimeError('shared selection')
    if any(x['text'].strip()!='831907' for x in [hit,fresh,isolated,mismatch]):raise RuntimeError('exact shared recall')
    resumed=request(plan,out,'session-priority',branch+hit['text']+turn('Reply with exactly 44.'),16,
        {'prefix_cache_scope':scope,'prefix_cache_write_tokens':8192,'session_id':child})
    if cached(resumed)<=8192 or resumed['text'].strip()!='44':raise RuntimeError('longer session seed skip')
    duplicate=request(plan,out,'duplicate-seed',prompt,8,{'prefix_cache_scope':scope,'prefix_cache_write_tokens':8192})
    if cached(duplicate)!=8192:raise RuntimeError('existing seed read')
    cancel_scope=capability('cancelled-seed')
    cancelled=request(plan,out,'cancel',prompt,256,{'prefix_cache_scope':cancel_scope,'prefix_cache_write_tokens':8192},True)
    deadline=time.monotonic()+30
    while time.monotonic()<deadline:
        check()
        with urllib.request.urlopen(f"http://127.0.0.1:{plan['port']}/healthz",timeout=5) as response:health=json.load(response)
        if health['seq_slots_free']==4 and health['gpu_healthy']:break
        time.sleep(.1)
    else:raise TimeoutError('cancelled seed slot release')
    after=request(plan,out,'after-cancel',branch,16,{'prefix_cache_scope':cancel_scope})
    if cached(after) or after['text'].strip()!='831907':raise RuntimeError('cancelled seed published')
    def rid(row):return next(m['id'] for m in row['messages'] if m.get('id'))
    records=[dict(re.findall(r'(\w+)=(\S+)',l)) for l in (out/'server.log').read_text().splitlines() if l.startswith('[prefix-cache] request=')]
    if (len(records)!=2 or {r['request'] for r in records}!={rid(source),rid(cancelled)} or
            any(r['prefix']!='8192' or r['error']!='none' for r in records)):
        raise RuntimeError('exact seed write policy')
    rejected_probes(plan,out)
    save(out/'lifecycle.json',dict(passed=True,requests=12,rejected_http=4,shared_tokens=cached(hit),
        session_tokens=cached(resumed),readonly_zero_writes=True,seed_saves=records,duplicate_no_write=True,cancel_unpublished=True))


def performance(plan,step,out,env,check):
    results=[]
    scenarios=plan['scenarios'] if step['block']==0 else list(reversed(plan['scenarios']))
    for scenario in scenarios:
        folder=out/scenario['id'];folder.mkdir()
        def one(i):
            key=str(step['block'])+scenario['id']
            extra={'session_id':capability(key+str(i))}
            if step['shared']:
                extra['prefix_cache_scope']=capability(key)
                if i==0:extra['prefix_cache_write_tokens']=scenario['prefix']
            return old.eval_one(plan,scenario['cases'][i],folder/str(i),env,check,extra)
        rows=[one(0)]
        with ThreadPoolExecutor(max_workers=3) as pool:rows+=list(pool.map(one,[1,2,3]))
        raw=(out/'server.log').read_text().splitlines()
        restores={r['request']:r for r in (dict(re.findall(r'(\w+)=(\S+)',l)) for l in raw if l.startswith('[session-cache] request=') and 'event=restore ' in l)}
        ids={r['response_id'] for r in rows}
        writes=[dict(re.findall(r'(\w+)=(\S+)',l)) for l in raw if l.startswith('[prefix-cache] request=')]
        writes=[r for r in writes if r['request'] in ids]
        if len(writes)!=int(step['shared']) or any(r['request']!=rows[0]['response_id'] or int(r['prefix'])!=scenario['prefix'] or r['error']!='none' for r in writes):raise RuntimeError('one parent seed, zero child writes')
        for i,row in enumerate(rows):
            entry=restores[row['response_id']]
            row['cached_tokens']=int(entry['prefix']);row['cache_source']=entry['source']
            expected=scenario['prefix'] if step['shared'] and i else 0
            if row['cached_tokens']!=expected:raise RuntimeError('exact shared match')
        item=dict(id=scenario['id'],block=step['block'],rows=rows,seed_writes=writes,
            makespan=max(r['end'] for r in rows)-min(r['start'] for r in rows),output_tokens=sum(r['output_tokens'] for r in rows))
        save(folder/'result.json',item);results.append(item)
    save(out/'results.json',results)


def custom_step(plan,name):
    legacy.require_guard()
    step=next(s for s in plan['steps'] if s['name']==name)
    out=Path(plan['output'])/name;out.mkdir()
    command=[step['binary'],'serve','--model-dir',plan['model'],'--host','127.0.0.1','--port',str(plan['port']),
        '--max-seq','4','--max-prefill','4096','--max-len','208896','--seq-capacities','208896,65536,65536,65536']
    if step['head']=='plain': command+=['--no-mtp']
    else:
        command+=['--mtp-suffix']
        if step.get('session',True):
            cache=out/'session';cache.mkdir(mode=0o700);command+=['--session-cache-dir',str(cache)]
        if step['shared']:
            cache=out/'prefix';cache.mkdir(mode=0o700);command+=['--prefix-cache-dir',str(cache)]
    if step['head']=='subset': command+=['--mtp-subset-head',plan['artifact']]
    env=legacy.clean_env();save(out/'server-command.json',dict(argv=command,environment={k:v for k,v in env.items() if k.startswith('Q4T_')}))
    save(out/'step.json',step)
    server=None;failure=None;passed=False
    deadline=min(time.time()+step['timeout']-45,plan['task_deadline_unix'])
    def check():
        if time.time()>=deadline: raise TimeoutError('step deadline')
        if server is not None and server.poll() is not None: raise RuntimeError('server exited')
    with (out/'server.log').open('x') as log:
        try:
            http.event(out,'server_before_start');server=subprocess.Popen(command,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT)
            http.event(out,'server_started',pid=server.pid)
            while 'serving on port' not in (out/'server.log').read_text(): check();time.sleep(1)
            if 'capacities=208896,65536,65536,65536' not in (out/'server.log').read_text(): raise RuntimeError('slot capacities')
            http.event(out,'server_ready',pid=server.pid)
            if plan['stage']=='PREFIX_A':
                if step.get('probe_only'):
                    rejected_probes(plan,out)
                else:
                    result=multi.case(plan,step,plan['quality_case'],out,env,check)
                    save(out/'first-http-gate.json',dict(passed=True,result=result,candidate_sha256=plan['candidate_sha256']))
                    if step['head']=='full':
                        with (out/'cpu-contract.log').open('x') as cpu:
                            code=subprocess.run([plan['storage_contract']],stdout=cpu,stderr=subprocess.STDOUT,timeout=90).returncode
                        if code: raise RuntimeError('CPU storage contract')
                    lifecycle(plan,out,check)
            elif plan['stage']=='PREFIX_C': performance(plan,step,out,env,check)
            elif plan['stage']=='PREFIX_D':
                results=[multi.case(plan,step,c,out,env,check) for c in plan['protection']]
                save(out/'results.json',results)
            else: raise ValueError('unimplemented HTTP phase')
            check();passed=True
        except BaseException as e: failure=repr(e);raise
        finally:
            if server is not None:
                http.event(out,'server_before_shutdown',pid=server.pid)
                if server.poll() is None:
                    server.terminate()
                    try: server.wait(timeout=30)
                    except subprocess.TimeoutExpired: server.kill();server.wait(timeout=10)
                http.event(out,'server_after_shutdown',pid=server.pid,server_returncode=server.returncode)
            save(out/'exit.json',dict(server=server.returncode if server else None,
                http_output_checks_passed=passed and server.returncode==0,failure=failure))



def audit(out,count):
    multi.lazy.admission._BASE_AUDIT(out,count)
    raw=(out/'server.log').read_text();lines=raw.splitlines()
    fields=lambda l:dict(re.findall(r'(\w+)=(\S+)',l))
    rejected=[fields(l) for l in lines if 'event=end ' in l and ' seq=-1 ' in l]
    probes=out/'rejected-probes.json'
    expected=4 if probes.exists() else 0
    if len(rejected)!=expected or any(r['outcome']!='error' or r['released']!='0' for r in rejected):raise RuntimeError('rejected request lifecycle')
    if rejected and sorted(r['id'] for r in rejected)!=['seed-reject-2','seed-reject-3','unregistered','unregistered']:raise RuntimeError('unrelated rejected request')
    instances={r['instance'] for r in rejected}
    filtered='\n'.join(l for l in lines if not (l.startswith('[q4t][request] ') and fields(l)['instance'] in instances))
    if count:
        result=old.session_audit.__globals__['runtime'](filtered,count)
    else:
        if not read_probe_only(out) or 'event=admitted ' in raw:
            raise RuntimeError('unplanned zero-generation service')
        tables=[fields(l) for l in lines if l.startswith('[suffix-classes] ')]
        if ([r['event'] for r in tables]!=['configure','release'] or
                any(int(tables[-1][k]) for k in ['decodes','checks','allocations'])):
            raise RuntimeError('probe-only service ran inference classification')
        result=dict(passed=True,requests=0)
    result['rejected']=rejected;result['original_log_unchanged']=True
    save(out/'session-audit.json',result)
    writes=[fields(l) for l in lines if l.startswith('[prefix-cache] request=')]
    if any(int(r['bytes'])>8*1024**3 or r['error']!='none' for r in writes):raise RuntimeError('seed IO/budget')
    save(out/'prefix-audit.json',dict(passed=True,saves=writes))


def read_probe_only(out):
    return json.loads((out/'step.json').read_text()).get('probe_only',False)


if __name__=='__main__':
    reuse.validate=old.validate;reuse.custom_step=custom_step;reuse.admission.audit_server=audit
    parser=argparse.ArgumentParser();parser.add_argument('--plan',type=Path,required=True);parser.add_argument('--custom-step')
    args=parser.parse_args();plan=json.loads(args.plan.read_text());old.validate(plan)
    prepare=None
    if plan['stage']=='PREFIX_C' and not args.custom_step:
        legacy.require_guard();legacy.verify_files(plan)
        spec=importlib.util.spec_from_file_location('seed_io',ROOT/'tools/prefix_seed/precondition.py')
        io=importlib.util.module_from_spec(spec);spec.loader.exec_module(io)
        prepare=io.prepare(args.plan.parent,plan['task_deadline_unix'])
    reuse.main()
    if prepare is not None:io.release(prepare,args.plan.parent)

"""Bounded prefix-sharing phases, using delivered HTTP and monitor ownership."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import json
from pathlib import Path
import pickle
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'tools/session_cache'))
spec = importlib.util.spec_from_file_location('session_base', ROOT/'tools/session_cache/run_stage.py')
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
multi, reuse, legacy, http, save = base.multi, base.reuse, base.legacy, base.http, base.save
from audit import audit_server as session_audit


def validate(plan):
    if plan['stage'] not in ['PREFIX_A', 'PREFIX_B', 'PREFIX_C', 'PREFIX_D', 'PREFIX_HEALTH']:
        raise ValueError('prefix stage')
    if sum(s['http_requests'] for s in plan['steps']) != plan['http_requests']:
        raise ValueError('request ledger')
    if sum(s['http_loads'] for s in plan['steps']) != plan['http_loads']:
        raise ValueError('start ledger')
    if time.time() >= plan['task_deadline_unix']:
        raise TimeoutError('absolute task deadline')
    for path in plan.get('required_gates', []):
        if not json.loads(Path(path).read_text())['passed']:
            raise ValueError('unpassed gate: ' + path)


def request(plan, out, name, prompt, tokens, extra=None, disconnect=False):
    body = dict(model='qwen3.8-flash-next', prompt=prompt, max_tokens=tokens,
                stream=True, stream_options={'include_usage': True}, temperature=0)
    body.update(extra or {})
    save(out/(name+'-request.json'), body)
    start=time.monotonic()
    req=urllib.request.Request(f"http://127.0.0.1:{plan['port']}/v1/chat/completions",
        data=json.dumps(body).encode(), headers={'Content-Type':'application/json'})
    text=''; messages=[]; first=None; usage=None; done=False
    with urllib.request.urlopen(req, timeout=600) as response:
        for raw in response:
            if not raw.startswith(b'data: '): continue
            value=raw[6:].strip()
            if value==b'[DONE]': done=True; break
            obj=json.loads(value); messages.append(obj)
            if 'error' in obj: raise RuntimeError(repr(obj))
            if obj.get('usage'): usage=obj['usage']
            for choice in obj.get('choices', []):
                content=choice.get('delta',{}).get('content','')
                if content and first is None: first=time.monotonic()
                text+=content
            if disconnect and first is not None: break
    row=dict(text=text, messages=messages, usage=usage, done=done,
        ttft=None if first is None else first-start, latency=time.monotonic()-start,
        disconnected=disconnect)
    save(out/(name+'-response.json'), row)
    if not disconnect and (not done or not usage): raise RuntimeError('HTTP terminal/usage')
    return row


def capability(text): return hashlib.sha256(text.encode()).hexdigest()
def cached(row): return row['usage'].get('prompt_tokens_details',{}).get('cached_tokens',0)
def turn(text):
    return ('<|im_end|>\n<|im_start|>user\n'+text+
            '<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n')


def lifecycle(plan, out, check):
    prompt=Path(plan['development_prompt']).read_text()
    scope=capability('prefix-A-scope'); child=capability('prefix-A-child')
    first=request(plan,out,'source',prompt,8,{'prefix_cache_scope':scope})
    if first['usage']['completion_tokens']!=8: raise RuntimeError('source length')
    request(plan,out,'overwrite',plan['short_prompt'],16)
    question='What is the audit_code in the authoritative shared record? Return only its six digits.'
    branch=prompt+first['text']+turn(question)
    hit=request(plan,out,'child',branch,16,{'prefix_cache_scope':scope,'session_id':child})
    fresh=request(plan,out,'fresh',branch,16)
    if cached(hit)!=8192 or hit['text'].strip()!='831907' or fresh['text'].strip()!='831907':
        raise RuntimeError('shared recall/fresh quality')
    isolated=request(plan,out,'isolated',branch,16,{'prefix_cache_scope':capability('other-scope')})
    mismatch=request(plan,out,'mismatch',branch.replace('archive','record',1),16,{'prefix_cache_scope':scope})
    if cached(isolated) or cached(mismatch) or any(r['text'].strip()!='831907' for r in [isolated,mismatch]):
        raise RuntimeError('scope/token isolation')
    resumed=request(plan,out,'session-priority',branch+hit['text']+turn('Reply with exactly 44.'),16,
                    {'prefix_cache_scope':scope,'session_id':child})
    if cached(resumed)<=8192 or resumed['text'].strip()!='44': raise RuntimeError('longer session priority')
    cancel_scope=capability('cancelled-prefix')
    request(plan,out,'cancel',prompt,256,{'prefix_cache_scope':cancel_scope},True)
    deadline=time.monotonic()+30
    while time.monotonic()<deadline:
        check()
        with urllib.request.urlopen(f"http://127.0.0.1:{plan['port']}/healthz",timeout=5) as r: health=json.load(r)
        if health['seq_slots_free']==4 and health['gpu_healthy']: break
        time.sleep(.1)
    else: raise TimeoutError('cancelled slot release')
    after=request(plan,out,'after-cancel',branch,16,{'prefix_cache_scope':cancel_scope})
    if cached(after) or after['text'].strip()!='831907': raise RuntimeError('cancel publication')
    save(out/'lifecycle.json',dict(passed=True,requests=9,shared_tokens=cached(hit),
         session_tokens=cached(resumed),isolated_miss=True,cancel_unpublished=True))


def eval_one(plan, case, out, env, check, extra):
    out.mkdir()
    command=[str(ROOT/'tools/evalscope/.venv/bin/evalscope'),'perf','--model','qwen3.8-flash-next',
        '--url',f"http://127.0.0.1:{plan['port']}/v1/chat/completions",'--api','openai',
        '--tokenizer-path',plan['model'],'--dataset','line_by_line','--dataset-path',case['path'],
        '--min-prompt-length',str(case['input_tokens']),'--max-prompt-length',str(case['input_tokens']),
        '--no-apply-chat-template','--max-tokens',str(case['tokens']),'--temperature','0','--seed','20260920',
        '--parallel','1','--number','1','--warmup-num','0','--no-test-connection','--stream',
        '--connect-timeout','30','--read-timeout','1200','--total-timeout','1500','--outputs-dir',str(out),
        '--extra-args',json.dumps(extra)]
    save(out/'command.json',command)
    before=http.event(out,'client_before',count=1)
    with (out/'client.log').open('x') as log:
        code=http.run_checked_client(command,env,log,check,out/'client-process.json')
    after=http.event(out,'client_after',returncode=code)
    databases=list(out.rglob('benchmark_data.db'))
    if code or len(databases)!=1: raise RuntimeError('evalscope collection')
    with sqlite3.connect(databases[0]) as db:
        db.row_factory=sqlite3.Row
        rows=list(db.execute('select * from result'))
    if len(rows)!=1: raise RuntimeError('HTTP ledger')
    r=rows[0]; messages=pickle.loads(base64.b64decode(r['response_messages']))
    choices=[c for m in messages for c in m.get('choices',[])]
    text=''.join(c.get('delta',{}).get('content','') for c in choices)
    ids={m['id'] for m in messages if m.get('id')}; body=json.loads(r['request'])
    result=dict(text=text,output_sha256=hashlib.sha256(text.encode()).hexdigest(),
        input_tokens=r['prompt_tokens'],output_tokens=r['completion_tokens'],success=r['success'],
        response_id=next(iter(ids)) if len(ids)==1 else None,start=r['start_time'],end=r['completed_time'],
        latency=r['latency'],ttft=r['first_chunk_latency'],
        finish=[c['finish_reason'] for c in choices if c.get('finish_reason')],
        timing=http.request_timing(r['start_time'],r['completed_time'],before,after),
        prompt_sha256=hashlib.sha256(body['prompt'].encode()).hexdigest())
    save(out/'response.json',result)
    if (not result['success'] or not result['response_id'] or result['input_tokens']!=case['input_tokens']
        or not result['timing']['within_client_boundaries'] or result['prompt_sha256']!=case['prompt_sha256']
        or any(body.get(k)!=v for k,v in extra.items())
        or result['finish'] not in [['length'],['stop']]): raise RuntimeError('HTTP performance contract')
    return result


def performance(plan, step, out, env, check):
    results=[]
    for scenario in plan['scenarios']:
        folder=out/scenario['id'];folder.mkdir()
        def one(i):
            extra={'session_id':capability(scenario['id']+str(i))}
            if step['shared']: extra['prefix_cache_scope']=capability(scenario['id'])
            return eval_one(plan,scenario['cases'][i],folder/str(i),env,check,extra)
        rows=[one(0)]
        with ThreadPoolExecutor(max_workers=3) as pool: rows+=list(pool.map(one,[1,2,3]))
        raw=(out/'server.log').read_text()
        restores={r['request']:r for r in (dict(re.findall(r'(\w+)=(\S+)',l)) for l in raw.splitlines()
            if l.startswith('[session-cache] request=') and 'event=restore ' in l)}
        for i,row in enumerate(rows):
            entry=restores[row['response_id']]
            row['cached_tokens']=int(entry['prefix']);row['cache_source']=entry['source']
            expected=scenario['prefix'] if step['shared'] and i else 0
            if row['cached_tokens']!=expected: raise RuntimeError('shared prefix match length')
        item=dict(id=scenario['id'],rows=rows,makespan=max(r['end'] for r in rows)-min(r['start'] for r in rows),
                  output_tokens=sum(r['output_tokens'] for r in rows))
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
                result=multi.case(plan,step,plan['quality_case'],out,env,check)
                save(out/'first-http-gate.json',dict(passed=True,result=result,candidate_sha256=plan['candidate_sha256']))
                if step['head']=='full':
                    with (out/'cpu-contract.log').open('x') as cpu:
                        code=subprocess.run([plan['storage_contract'],str(out/'cpu-cache')],stdout=cpu,stderr=subprocess.STDOUT,timeout=90).returncode
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
    session_audit(out,count)
    raw=(out/'server.log').read_text()
    saves=[dict(re.findall(r'(\w+)=(\S+)',l)) for l in raw.splitlines() if l.startswith('[prefix-cache] request=')]
    if any(int(r['bytes'])>8*1024**3 for r in saves):
        raise RuntimeError('shared IO/budget error')
    save(out/'prefix-audit.json',dict(passed=True,saves=saves))


if __name__=='__main__':
    reuse.validate=validate;reuse.custom_step=custom_step;reuse.admission.audit_server=audit
    reuse.main()

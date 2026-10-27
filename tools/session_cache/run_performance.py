"""Frozen four-agent session transactions and five-tier protection E2E."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import pickle
import re
import sqlite3
import subprocess
import threading
import time
import urllib.request

from run_stage import ROOT, multi, reuse, legacy, http, save, request
from audit import audit_server
from oracle import short_answer


def validate(plan):
    phase = plan['stage']
    if phase not in ['SESSION_C', 'SESSION_D']:
        raise ValueError('performance scope')
    count, starts = (98, 4) if phase == 'SESSION_C' else (90, 6)
    if (plan['http_requests'], plan['http_loads']) != (count, starts):
        raise ValueError('frozen request/start budget')
    if len(plan['steps']) != starts or sum(s['http_requests'] for s in plan['steps']) != count:
        raise ValueError('step ledger')
    if time.time() >= plan['task_deadline_unix']:
        raise TimeoutError('absolute deadline')
    for p in plan['required_gates']:
        record = json.loads(Path(p).read_text())
        if not record['passed']:
            raise ValueError('unpassed dependency: ' + p)


def one_case(plan, c, out, env, check, key):
    out.mkdir()
    command = [str(ROOT/'tools/evalscope/.venv/bin/evalscope'), 'perf',
        '--model', 'qwen3.8-flash-next', '--url',
        f"http://127.0.0.1:{plan['port']}/v1/chat/completions", '--api', 'openai',
        '--tokenizer-path', plan['model'], '--dataset', 'line_by_line',
        '--dataset-path', c['path'], '--min-prompt-length', str(c['input_tokens']),
        '--max-prompt-length', str(c['input_tokens']), '--no-apply-chat-template',
        '--max-tokens', str(c['tokens']), '--temperature', '0', '--seed', '20260920',
        '--parallel', '1', '--number', '1', '--warmup-num', '0',
        '--no-test-connection', '--stream', '--connect-timeout', '30',
        '--read-timeout', '1200', '--total-timeout', '1500',
        '--outputs-dir', str(out)]
    if key:
        command += ['--extra-args', json.dumps({'session_id': key})]
    save(out/'command.json', command)
    before = http.event(out, 'client_before', count=1)
    with (out/'client.log').open('x') as log:
        code = http.run_checked_client(command, env, log, check, out/'client-process.json')
    after = http.event(out, 'client_after', returncode=code)
    databases = list(out.rglob('benchmark_data.db'))
    if code or len(databases) != 1:
        raise RuntimeError('evalscope collection failed: ' + str(out))
    with sqlite3.connect(databases[0]) as db:
        db.row_factory = sqlite3.Row
        records = list(db.execute('select * from result'))
    if len(records) != 1:
        raise RuntimeError('one request ledger')
    r = records[0]
    # Only decode the trusted local database produced by this invocation.
    messages = pickle.loads(base64.b64decode(r['response_messages']))
    choices = [c for m in messages for c in m.get('choices', [])]
    text = ''.join(c.get('delta', {}).get('content', '') for c in choices)
    ids = {m['id'] for m in messages if m.get('id')}
    body = json.loads(r['request'])
    result = dict(text=text, output_sha256=hashlib.sha256(text.encode()).hexdigest(),
        input_tokens=r['prompt_tokens'], output_tokens=r['completion_tokens'],
        success=r['success'], response_id=next(iter(ids)) if len(ids)==1 else None,
        start=r['start_time'], end=r['completed_time'], latency=r['latency'],
        ttft=r['first_chunk_latency'],
        finish=[c['finish_reason'] for c in choices if c.get('finish_reason')],
        timing=http.request_timing(r['start_time'],r['completed_time'],before,after),
        request_sha256=hashlib.sha256(body['prompt'].encode()).hexdigest(),
        session_sent=body.get('session_id') == key if key else 'session_id' not in body)
    save(out/'response.json',result)
    if (not result['success'] or not result['response_id'] or
            not result['session_sent'] or result['finish'] != ['length'] or
            result['output_tokens'] != c['tokens'] or
            result['input_tokens'] != c['input_tokens'] or
            not result['timing']['within_client_boundaries'] or
            result['request_sha256'] != c['prompt_sha256'] or
            (c.get('expected_sha256') and
             result['output_sha256'] != c['expected_sha256'])):
        raise RuntimeError('frozen HTTP contract failed: ' + str(out))
    return result


def group(plan, step, scenario, round_name, out, env, check):
    directory = out / (scenario['id'] + '-' + round_name)
    directory.mkdir()
    failed = threading.Event()
    def checked():
        check()
        if failed.is_set():
            raise RuntimeError('owned sibling client failed')
    with ThreadPoolExecutor(max_workers=4) as workers:
        futures = []
        for slot, c in enumerate(scenario[round_name]):
            key = None
            if step['cache'] and round_name != 'interference':
                key = hashlib.sha256(f"session-perf:{scenario['id']}:{slot}".encode()).hexdigest()
            futures.append(workers.submit(one_case, plan, c, directory/str(slot),
                                            env, checked, key))
        try:
            rows = [f.result() for f in futures]
        except BaseException:
            failed.set()
            raise
    result = dict(id=scenario['id'], round=round_name, rows=rows,
        makespan=max(r['end'] for r in rows)-min(r['start'] for r in rows),
        total_output=sum(r['output_tokens'] for r in rows))
    save(directory/'result.json',result)
    raw = (out/'server.log').read_text()
    field = lambda line: dict(re.findall(r'(\w+)=(\S+)',line))
    admissions = {r['request']:r for r in (field(l) for l in raw.splitlines()
        if l.startswith('[q4t][slot-admission] '))}
    if {int(admissions[r['response_id']]['slot']) for r in rows} != {0,1,2,3}:
        raise RuntimeError('four physical slots not covered')
    if step['cache'] and round_name == 'second':
        restores = {r['request']:r for r in (field(l) for l in raw.splitlines()
            if l.startswith('[session-cache] ') and 'event=restore ' in l)}
        for row in rows:
            if int(restores[row['response_id']]['prefix']) < 8192:
                raise RuntimeError('session prefix was not restored')
    return result


def cancellation_recovery(plan, out, check):
    key = hashlib.sha256(b'session-full-cancel-recovery').hexdigest()
    request(plan,out,'cancel',Path(plan['session_prompt']).read_text(),256,key,
              disconnect=True)
    deadline = time.monotonic()+30
    while time.monotonic() < deadline:
        check()
        with urllib.request.urlopen(f"http://127.0.0.1:{plan['port']}/healthz",timeout=5) as r:
            health=json.load(r)
        if health['seq_slots_free']==4 and health['gpu_healthy']:
            break
        time.sleep(.5)
    else:
        raise TimeoutError('cancel release')
    result=request(plan,out,'after-cancel',plan['short_prompt'],16,key)
    if result['text'].strip() != short_answer(plan):
        raise RuntimeError('same-session recovery quality')


def custom_step(plan, name):
    legacy.require_guard()
    step=next(s for s in plan['steps'] if s['name']==name)
    out=Path(plan['output'])/name
    out.mkdir()
    command=[step['binary'],'serve','--model-dir',plan['model'],'--host','127.0.0.1',
        '--port',str(plan['port']),'--max-seq','4','--max-prefill','4096',
        '--max-len','208896','--seq-capacities','208896,65536,65536,65536']
    if step['head']=='plain':
        command+=['--no-mtp']
    else:
        command+=['--mtp-suffix']
    if step['head']=='subset':
        command+=['--mtp-subset-head',plan['artifact']]
    if step['cache']:
        cache=out/'cache';cache.mkdir(mode=0o700)
        command+=['--session-cache-dir',str(cache)]
    env=legacy.clean_env()
    save(out/'server-command.json',dict(argv=command,environment={
        k:v for k,v in env.items() if k.startswith('Q4T_')}))
    save(out/'step.json',step)
    server=None;failure=None;passed=False
    deadline=min(time.time()+step['timeout']-45,plan['task_deadline_unix'])
    def check():
        if time.time()>=deadline:raise TimeoutError('frozen step/deadline')
        if server is not None and server.poll() is not None:raise RuntimeError('server exited')
    with (out/'server.log').open('x') as log:
        try:
            http.event(out,'server_before_start')
            server=subprocess.Popen(command,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT)
            http.event(out,'server_started',pid=server.pid)
            while 'serving on port' not in (out/'server.log').read_text():
                check();time.sleep(1)
            raw=(out/'server.log').read_text()
            if 'capacities=208896,65536,65536,65536' not in raw:
                raise RuntimeError('capacity changed')
            http.event(out,'server_ready',pid=server.pid)
            results=[]
            if plan['stage']=='SESSION_C':
                for scenario in plan['scenarios']:
                    for round_name in ['first','interference','second']:
                        results.append(group(plan,step,scenario,round_name,out,env,check))
                        save(out/'results.json',results)
                if step.get('cancel_recovery'):
                    cancellation_recovery(plan,out,check)
            else:
                for c in plan['protection']:
                    results.append(multi.case(plan,step,c,out,env,check))
                    save(out/'results.json',results)
            check();passed=True
        except BaseException as error:
            failure=repr(error);raise
        finally:
            if server is not None:
                http.event(out,'server_before_shutdown',pid=server.pid)
                if server.poll() is None:
                    server.terminate()
                    try:server.wait(timeout=30)
                    except subprocess.TimeoutExpired:server.kill();server.wait(timeout=10)
                http.event(out,'server_after_shutdown',pid=server.pid,server_returncode=server.returncode)
            save(out/'exit.json',dict(server=server.returncode if server else None,
                http_output_checks_passed=passed and server is not None and server.returncode==0,
                failure=failure))


if __name__=='__main__':
    reuse.validate=validate
    reuse.custom_step=custom_step
    reuse.admission.audit_server=audit_server
    reuse.main()

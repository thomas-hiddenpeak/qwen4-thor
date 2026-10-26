"""Bounded four-active-slot HTTP stage; reuse the delivered lease/monitor."""
import base64
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import pickle
import re
import signal
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    'multi_lazy', ROOT / 'tools/mtp_suffix_lazy/run_stage.py')
lazy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lazy)
reuse, legacy, http = lazy.reuse, lazy.legacy, lazy.http
save = legacy.save


def validate(plan):
    resumed=bool(plan.get('first_http_reuse'))
    requests,starts=(108,3) if resumed else (136,4)
    if plan['stage'] != 'F' or (plan['http_requests'], plan['http_loads']) != (requests,starts):
        raise ValueError('frozen F scope')
    if not 0 < plan['stage_timeout_seconds'] <= 7200:
        raise ValueError('F remaining wall budget')
    if time.time() >= plan['task_deadline_unix']:
        raise TimeoutError('absolute stage deadline')
    services = [s for s in plan['steps'] if s['kind'] == 'block']
    if len(services) != starts or sum(s['http_requests'] for s in services) != requests:
        raise ValueError('frozen request/start ledger')
    for s in services:
        if s['http_requests'] != sum(c['count'] for c in s['cases']) + 3*s['suffix']:
            raise ValueError('case request ledger')
    if resumed:
        gate=Path(plan['first_http_reuse'])
        if legacy.sha(gate)!=plan['first_http_reuse_sha256'] or not json.loads(gate.read_text())['passed']:
            raise ValueError('first HTTP reuse identity')
        if plan['candidate_sha256']!=plan['reused_candidate_sha256']:
            raise ValueError('inference changed since first HTTP')
    elif services[0]['cases'][0]['id'] != 'quality':
        raise ValueError('HTTP first gate')


def case(plan, step, c, out, env, check):
    directory = out / c['id']
    directory.mkdir()
    command = [str(ROOT / 'tools/evalscope/.venv/bin/evalscope'), 'perf',
        '--model', 'qwen3.8-flash-next', '--url',
        f"http://127.0.0.1:{plan['port']}/v1/chat/completions", '--api', 'openai',
        '--tokenizer-path', plan['model'], '--dataset', 'line_by_line',
        '--dataset-path', c['path'], '--min-prompt-length', str(c['min']),
        '--max-prompt-length', str(c['max']), '--no-apply-chat-template',
        '--max-tokens', str(c['tokens']), '--temperature', '0', '--seed',
        '20260920', '--parallel', str(c['parallel']), '--number', str(c['count']),
        '--warmup-num', '0', '--no-test-connection', '--stream',
        '--connect-timeout', '30', '--read-timeout', '600', '--total-timeout',
        '1200', '--outputs-dir', str(directory)]
    save(directory / 'command.json', command)
    before = http.event(out, 'client_before', case=c['id'], count=c['count'])
    with (directory / 'client.log').open('x') as log:
        code = http.run_checked_client(command, env, log, check,
                                       directory / 'client-process.json')
    after = http.event(out, 'client_after', case=c['id'], returncode=code)
    databases = list(directory.rglob('benchmark_data.db'))
    rows = []
    if len(databases) == 1:
        with sqlite3.connect(databases[0]) as db:
            db.row_factory = sqlite3.Row
            for r in db.execute('select * from result order by start_time'):
                # Only this run's trusted local client database is decoded.
                messages = pickle.loads(base64.b64decode(r['response_messages']))
                choices = [c for m in messages for c in m.get('choices', [])]
                text = ''.join(c.get('delta', c.get('message', {})).get(
                    'content', '') for c in choices)
                request = json.loads(r['request'])
                ids = {m['id'] for m in messages if m.get('id')}
                rows.append(dict(success=r['success'], text=text,
                    output_sha256=hashlib.sha256(text.encode()).hexdigest(),
                    prompt_sha256=hashlib.sha256(request['prompt'].encode()).hexdigest(),
                    response_id=next(iter(ids)) if len(ids) == 1 else None,
                    input_tokens=r['prompt_tokens'], output_tokens=r['completion_tokens'],
                    latency=r['latency'], ttft=r['first_chunk_latency'],
                    start=r['start_time'], end=r['completed_time'],
                    finish=[c['finish_reason'] for c in choices if c.get('finish_reason')],
                    requested_max_tokens=request.get('max_tokens'),
                    stream=request.get('stream'),
                    timing=http.request_timing(r['start_time'], r['completed_time'], before, after)))
    save(directory / 'responses.json', rows)
    if code or len(rows) != c['count'] or len({r['response_id'] for r in rows}) != len(rows):
        raise RuntimeError('HTTP client/count/identity: ' + c['id'])
    for r in rows:
        if (not r['success'] or not r['response_id'] or not r['stream'] or
                r['requested_max_tokens'] != c['tokens'] or
                not c['min'] <= r['input_tokens'] <= c['max'] or
                not r['timing']['within_client_boundaries'] or
                any(not isinstance(r[k], (int, float)) or not math.isfinite(r[k])
                    or r[k] <= 0 for k in ['latency', 'ttft'])):
            raise RuntimeError('HTTP metric/input/stream contract: ' + c['id'])
        if c['id'] == 'quality':
            expected = plan['quality'][r['prompt_sha256']]
            if (r['text'].strip() != expected['expected'] or
                    r['input_tokens'] != expected['length'] or r['finish'] != ['stop']):
                raise RuntimeError('HTTP quality mismatch')
        elif r['output_tokens'] != c['tokens'] or r['finish'] != ['length']:
            raise RuntimeError('HTTP output length/finish: ' + c['id'])
    if c['id'] == 'quality' and {r['prompt_sha256'] for r in rows} != set(plan['quality']):
        raise RuntimeError('quality prompt coverage')
    result = dict(id=c['id'], rows=rows,
        makespan_seconds=max(r['end'] for r in rows)-min(r['start'] for r in rows),
        total_output_tokens=sum(r['output_tokens'] for r in rows))
    result['throughput'] = result['total_output_tokens'] / result['makespan_seconds']
    save(directory / 'result.json', result)
    return result


def custom_step(plan, name):
    legacy.require_guard()
    step = next(s for s in plan['steps'] if s['name'] == name)
    out = Path(plan['output']) / name
    out.mkdir()
    env = {k:v for k,v in legacy.clean_env().items() if not k.startswith('CUPTI_')}
    command = [plan['candidate'], 'serve', '--model-dir', plan['model'],
        '--host', '127.0.0.1', '--port', str(plan['port']), '--max-seq', '4',
        '--max-prefill', '8192', '--max-len', '16384', '--max-tokens', '256',
        '--mtp', '--mtp-k', '3']
    if step['head'] == 'subset': command += ['--mtp-subset-head', plan['artifact']]
    if step['suffix']: command += ['--mtp-suffix']
    save(out/'server-command.json', dict(argv=command, environment={
        k:v for k,v in env.items() if k.startswith('Q4T_')}))
    save(out/'step.json', step)
    server, failure, passed = None, None, False
    deadline = time.monotonic() + step['timeout'] - 45
    def check():
        if time.monotonic() >= deadline or time.time() >= plan['task_deadline_unix']:
            raise TimeoutError('bounded concurrent HTTP step')
        if server is not None and server.poll() is not None:
            raise RuntimeError('server exited before shutdown')
    with (out/'server.log').open('x') as log:
        try:
            http.event(out, 'server_before_start')
            server = subprocess.Popen(command, cwd=ROOT, env=env,
                stdout=log, stderr=subprocess.STDOUT)
            (out/'server.pid').write_text(str(server.pid)+'\n')
            http.event(out, 'server_started', pid=server.pid)
            startup = min(deadline, time.monotonic()+600)
            while time.monotonic() < startup:
                check()
                if 'serving on port' in (out/'server.log').read_text(): break
                time.sleep(1)
            else: raise TimeoutError('concurrent startup')
            raw = (out/'server.log').read_text()
            lines = re.findall(r'\[q4t\]\[capacity\] ([^\n]+)', raw)
            if len(lines) != 1: raise RuntimeError('capacity receipt absent')
            capacity = dict(re.findall(r'(\w+)=(\S+)', lines[0]))
            save(out/'capacity.json', capacity)
            for prefix in ['requested', 'effective']:
                for key,value in [('max_seq',4),('max_len',16384),('max_prefill',8192)]:
                    if int(capacity[prefix+'_'+key]) != value:
                        raise RuntimeError('capacity silently reduced')
            http.event(out, 'server_ready', pid=server.pid, capacity=capacity)
            results=[]
            for c in step['cases']:
                results.append(case(plan,step,c,out,env,check))
                save(out/'results.json',results)
                if name == plan['steps'][0]['name'] and c['id']=='quality':
                    save(Path(plan['output'])/'first-http-gate.json', dict(
                        passed=True, requests=9, unix=time.time(), head='full',
                        suffix=True, max_seq=4))
                    for n,contract in enumerate(plan['host_contracts']):
                        with (out/f'host-{n}.log').open('x') as clog:
                            subprocess.run(contract,cwd=ROOT,env=env,stdout=clog,
                                stderr=subprocess.STDOUT,check=True,timeout=120)
            if step['suffix']:
                save(out/'lifecycle-results.json', lazy.admission.lifecycle_admission(
                    plan,step,out,expected_slots=4))
            check(); passed=True
        except BaseException as error:
            failure=repr(error); raise
        finally:
            if server is not None:
                http.event(out,'server_before_shutdown',pid=server.pid)
                if server.poll() is None:
                    server.terminate()
                    try: server.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        server.kill();server.wait(timeout=10)
                http.event(out,'server_after_shutdown',pid=server.pid,
                           server_returncode=server.returncode)
            save(out/'exit.json',dict(server=server.returncode if server else None,
                http_output_checks_passed=passed and server.returncode==0,
                failure=failure))
    if not passed or server.returncode: raise RuntimeError('unclean HTTP step')


def audit_server(out, count):
    lazy.admission._BASE_AUDIT(out,count)
    text=(out/'server.log').read_text()
    fields=lambda line:dict(re.findall(r'(\w+)=(\S+)',line))
    admitted=[fields(l) for l in text.splitlines() if 'event=admitted ' in l]
    ended=[fields(l) for l in text.splitlines() if 'event=end ' in l]
    if (len(ended)!=count or {r['id'] for r in admitted}!={r['id'] for r in ended}
            or any(r.get('released')!='1' or r.get('outcome') not in ['success','cancelled'] for r in ended)):
        raise RuntimeError('concurrent request release/failure')
    suffix=[fields(l) for l in text.splitlines() if l.startswith('[suffix] ')]
    for r in suffix:
        lazy.audit_stats(r)
        if r['request'].endswith('-cancel') and (int(r['hits'])<=0 or not any(
                e['id']==r['request'] and e.get('outcome')=='cancelled' and
                e.get('reason')=='explicit_cancel' for e in ended)):
            raise RuntimeError('cancel after suffix use required')
    table=[fields(l) for l in text.splitlines() if l.startswith('[suffix-classes] ')]
    if suffix:
        if len(table)!=2 or [r['event'] for r in table]!=['configure','release']:
            raise RuntimeError('cache lifetime')
        for key,local in [('decodes','cache_decodes'),('lookups','cache_lookups'),
                          ('allocations','cache_allocations'),('checks','numeric_checks')]:
            if int(table[-1][key]) != sum(int(r[local]) for r in suffix):
                raise RuntimeError('per-request cache attribution: '+key)
    elif table: raise RuntimeError('cache work while disabled')
    batches=[{k:int(v) for k,v in fields(l).items()} for l in text.splitlines()
             if l.startswith('[mtp-batches] ')]
    if not batches or max(r['rows'] for r in batches)!=4:
        raise RuntimeError('actual four-row MTP batch not covered')
    if suffix and not any(0<r['suffix']<r['rows'] for r in batches):
        raise RuntimeError('actual mixed batch not covered')
    if any(not 0<=r['suffix']<=r['rows']<=4 or r['steps']<=0 for r in batches):
        raise RuntimeError('batch work ledger')
    if sum(r['steps']*r['suffix'] for r in batches)!=sum(int(r['hits']) for r in suffix):
        raise RuntimeError('suffix rows and request hits disagree')
    accepted=[fields(l) for l in text.splitlines() if l.startswith('[mtp-accept] ')]
    if len(accepted)!=count or sum(sum(map(int,r['draft_counts'].split(','))) for r in accepted)!=sum(r['steps']*r['rows'] for r in batches):
        raise RuntimeError('verify rows and request acceptance disagree')
    save(out/'multi-audit.json',dict(passed=True,batches=batches,suffix=suffix,
        accepted=accepted,lifecycle=ended,cache=table))


if __name__=='__main__':
    def interrupted(*_): raise KeyboardInterrupt('owned multi-stage interrupted')
    signal.signal(signal.SIGTERM,interrupted)
    reuse.validate=validate
    reuse.custom_step=custom_step
    reuse.admission.audit_server=audit_server
    reuse.main()

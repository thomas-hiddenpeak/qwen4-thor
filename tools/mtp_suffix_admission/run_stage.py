"""One bounded minimum-admission stage beneath the shared outer GPU lease.

The original suffix driver stays unchanged. Its resource/process lifecycle is
reused with a stricter audit callback; custom HTTP still uses evalscope.run_case.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    'q4t_frozen_suffix_stage', ROOT / 'tools/mtp_suffix/run_stage.py')
legacy = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(legacy)
http_runner = legacy.http_runner
save = legacy.save
_BASE_AUDIT = legacy.audit_server


def audit_server(output, expected_requests):
    """Reject old rules, missing evidence, fallback, or inconsistent counters."""
    _BASE_AUDIT(output, expected_requests)
    text = (output / 'server.log').read_text()
    for marker in ['MTP trunk alloc failed', 'plain decode for this request',
                   'MTP load failed', 'MTP init failed', 'MTP allocation failed']:
        if marker in text:
            raise RuntimeError('MTP fallback invalidates this arm: ' + marker)
    argv = json.loads((output / 'server-command.json').read_text())['argv']
    enabled = '--mtp-suffix' in argv
    table = [dict(line=line_number, **dict(re.findall(r'(\w+)=(\S+)', line)))
             for line_number,line in enumerate(text.splitlines(),1)
             if line.startswith('[suffix-classes] ')]
    summaries = json.loads((output / 'server-audit.json').read_text())['suffix']
    admitted, ended = {}, {}
    for number, line in enumerate(text.splitlines(), 1):
        if 'event=admitted ' not in line and 'event=end ' not in line:
            continue
        row = dict(re.findall(r'(\w+)=(\S+)', line))
        target = admitted if row.get('event') == 'admitted' else ended
        identity = row.get('id')
        if not identity or identity in target:
            raise RuntimeError('missing/duplicate request lifecycle identity')
        target[identity] = dict(row, line=number)
    if (len(admitted) != expected_requests or admitted.keys() != ended.keys() or
            any(row.get('released') != '1' or row['line'] <= admitted[key]['line']
                for key, row in ended.items())):
        raise RuntimeError('request end/slot-release lifecycle is incomplete')
    if not enabled:
        if table:
            raise RuntimeError('suffix-off arm initialized the classification table')
        return
    init = [r for r in table if r.get('event') == 'init']
    release = [r for r in table if r.get('event') == 'release']
    enabled_lines = [line for line in text.splitlines() if 'suffix enabled:' in line]
    if (len(init) != 1 or len(release) != 1 or len(table) != 2 or
            len(enabled_lines) != 1):
        raise RuntimeError('missing/duplicate admission table lifecycle')
    enabled_fields = dict(re.findall(r'(\w+)=(\S+)', enabled_lines[0]))
    if (enabled_fields.get('numeric_gate') != '1' or
            enabled_fields.get('min_admitted_match') != '32'):
        raise RuntimeError('wrong admission rule; numeric-only old binary is not eligible')
    suffix_lines = [i for i, line in enumerate(text.splitlines(), 1)
                    if line.startswith('[suffix] ')]
    if (init[0]['line'] >= min(r['line'] for r in admitted.values()) or
            release[0]['line'] <= max(
                [r['line'] for r in ended.values()] + suffix_lines)):
        raise RuntimeError('classification table lifetime does not enclose requests')
    if {r.get('request') for r in summaries} != admitted.keys():
        raise RuntimeError('duplicate or missing suffix request identity')
    try:
        if (int(init[0]['bytes']) != 248320 or int(init[0]['cap_bytes']) != 262144 or
                int(init[0]['unknown']) != 243 or int(init[0]['init_ns']) < 0 or
                int(release[0]['bytes']) != 248320 or int(release[0]['release_ns']) < 0):
            raise ValueError('table size/capacity/decoder domain mismatch')
        for row in summaries:
            values = {key: int(row[key]) for key in
                      ['queries','hits','peak_bytes','query_ns','numeric_checks',
                       'classified_tokens','numeric_rejected','short_rejected','numeric_ns',
                       'no_match','incomplete','d0_mismatch']}
            if any(v < 0 for v in values.values()) or not 0 < values['peak_bytes'] <= 2*1024*1024:
                raise ValueError('invalid suffix resource/counter value')
            checks = values['numeric_checks']
            short = values['short_rejected']
            rejected = values['numeric_rejected']
            classified_min = 32*values['hits'] + 3*(rejected+short)
            classified_max = 64*(values['hits']+rejected) + 31*short
            outcomes = sum(values[k] for k in ['hits','no_match','incomplete',
                                              'd0_mismatch','numeric_rejected','short_rejected'])
            if (checks != values['hits'] + rejected + short or
                    checks > values['queries'] or
                    outcomes != values['queries'] or
                    not classified_min <= values['classified_tokens'] <= classified_max or
                    values['numeric_ns'] > values['query_ns']):
                raise ValueError('admission outcomes, scan bounds or nested timing are inconsistent')
            if row['request'].endswith('-cancel') and (
                    values['hits'] <= 0 or
                    ended[row['request']].get('outcome') != 'cancelled' or
                    ended[row['request']].get('reason') != 'explicit_cancel'):
                raise ValueError('explicit cancellation did not follow actual suffix use')
    except (KeyError, ValueError) as error:
        raise RuntimeError('minimum-admission evidence invalid: ' + str(error)) from error
    save(output / 'admission-audit.json', dict(table=table, requests=summaries,
         cost_scope='numeric_ns is included in query_ns; never add both',
         min_admitted_match=32, table_bytes=248320, table_cap_bytes=262144,
         request_index_cap_bytes=2*1024*1024))


def lifecycle_admission(plan, step, output, expected_slots=1):
    """Three requests; cancel after 64 content chunks, then inspect real suffix use."""
    scenario = next(c for c in plan['development'] if c['id'] == 'old-repeat-code')
    prompt = json.loads(Path(scenario['path']).read_text().splitlines()[0])['prompt']
    progress = {'nonempty_chunks': 0}
    minimum_chunks = 64

    def request(path, payload=None, content_ready=None, label=None):
        connection = http.client.HTTPConnection('127.0.0.1', plan['port'], timeout=180)
        if label:
            save(output / (label + '-request.json'), payload)
        try:
            connection.request('GET' if payload is None else 'POST', path,
                None if payload is None else json.dumps(payload),
                {'Content-Type': 'application/json'})
            response = connection.getresponse()
            raw = bytearray()
            while True:
                line = response.readline()
                if not line:
                    break
                raw.extend(line)
                if content_ready is not None and line.startswith(b'data: {'):
                    message = json.loads(line[6:])
                    if any(c.get('delta', {}).get('content')
                           for c in message.get('choices', [])):
                        progress['nonempty_chunks'] += 1
                        if progress['nonempty_chunks'] >= minimum_chunks:
                            content_ready.set()
            result = dict(status=response.status, body=raw.decode())
            if label:
                save(output / (label + '-response.json'), result)
            return result
        finally:
            connection.close()

    def payload(label):
        return dict(model='qwen3.8-flash-next', prompt=prompt, temperature=0,
                    max_tokens=256, stream=True, stream_options={'include_usage': True},
                    request_id=label,
                    cancel_token=hashlib.sha256(label.encode()).hexdigest())

    def complete(response, label):
        value = legacy.response_fields(legacy.parse_sse(response, label))
        usage = dict(prompt_tokens=scenario['input_tokens'], completion_tokens=256,
                     total_tokens=scenario['input_tokens'] + 256)
        if (value['errors'] or not value['text'] or value['finish'] != ['length'] or
                value['usage'] != [usage]):
            raise RuntimeError('lifecycle normal response failed: ' + label)
        return value

    def idle():
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            result = request('/healthz')
            if result['status'] != 200:
                raise RuntimeError('health failed after lifecycle request')
            value = json.loads(result['body'])
            if value['seq_slots_free'] == expected_slots:
                if not value['gpu_healthy'] or value['seq_slots_total'] != expected_slots:
                    raise RuntimeError('lifecycle GPU health or capacity failed')
                return value
            time.sleep(.05)
        raise RuntimeError('lifecycle slot failed to drain')

    label = 'suffix-' + step['head'] + '-control'
    control = complete(request('/v1/chat/completions', payload(label), label=label), label)
    idle()
    label = 'suffix-' + step['head'] + '-cancel'
    body, content_ready = payload(label), threading.Event()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(request, '/v1/chat/completions', body, content_ready, label)
        if not content_ready.wait(90):
            raise RuntimeError('fewer than 64 content chunks before cancellation deadline')
        observed_before_cancel = progress['nonempty_chunks']
        http_runner.event(output, 'cancel_content_threshold', request_id=label,
                          required_nonempty_chunks=minimum_chunks,
                          observed_nonempty_chunks=observed_before_cancel)
        cancelled = request('/v1/requests/cancel',
            {key: body[key] for key in ['request_id', 'cancel_token']})
        save(output / 'cancel-control-response.json', cancelled)
        if (cancelled['status'] != 202 or
                json.loads(cancelled['body']).get('cancel_requested') is not True):
            raise RuntimeError('explicit cancellation was not accepted')
        legacy.require_cancelled(future.result(timeout=120), label)
    if observed_before_cancel < minimum_chunks:
        raise RuntimeError('cancellation preceded the fixed content threshold')
    save(output / 'after-cancel-health.json', idle())
    label = 'suffix-' + step['head'] + '-recovery'
    recovery = complete(request('/v1/chat/completions', payload(label), label=label), label)
    if recovery != control:
        raise RuntimeError('fresh same-mode recovery differs from control')
    save(output / 'after-recovery-health.json', idle())
    return dict(requests=3, control=control, recovery=recovery,
                cancellation=dict(required_nonempty_chunks=minimum_chunks,
                                  observed_nonempty_chunks_before_cancel=observed_before_cancel,
                                  final_nonempty_chunks=progress['nonempty_chunks'],
                                  actual_suffix_hit_required_after_server_shutdown=True),
                slots=expected_slots,
                scope='cancel after 64 nonempty content chunks and actual suffix use; fresh request recovery; no overlapping generation in this lifecycle fixture')


def validate_plan(plan):
    steps = plan['steps']
    if (plan['http_requests'] != 250 or plan['http_loads'] != 18 or
            plan['stage_timeout_seconds'] != 14400 or
            plan['direct_contract_timeout_seconds'] != 1200 or
            sum(s['http_requests'] for s in steps) != 250 or
            sum(s['http_loads'] for s in steps) != 18 or
            steps[0]['name'] != 'quality-full-on' or
            steps[0]['kind'] != 'quality' or steps[0]['http_requests'] != 11 or
            steps[0]['head'] != 'full' or steps[0]['suffix'] is not True):
        raise ValueError('plan differs from frozen admission-stage budget/first HTTP gate')
    if plan.get('admission') != dict(min_admitted_match=32,lookup_min_match=3,max_match=64,
            candidates=4,order='complete-three, d0, numeric, minimum-match',
            alternative_candidate_search=False,dynamic_policy=False):
        raise ValueError('plan differs from the single frozen admission rule')
    blocks = [s for s in steps if s['kind'] == 'block']
    if len(blocks) != 8 or any(s['http_requests'] != 23 for s in blocks):
        raise ValueError('two-block full/subset matrix is incomplete')
    expected = [(1,'full',False),(1,'full',True),(1,'subset',True),(1,'subset',False),
                (2,'full',True),(2,'full',False),(2,'subset',False),(2,'subset',True)]
    if [(s['block'],s['head'],s['suffix']) for s in blocks] != expected:
        raise ValueError('within-head AB/BA startup order changed')
    for step in blocks:
        cases = step['cases']
        if ([c['id'] for c in cases] !=
                [f'context-{n}' for n in [1024,4096,8192,45056,204800]] +
                ['repeat-code','repeat-text','nonrepeat','numeric-enum',
                 'old-8k','old-45k','old-repeat-code','old-repeat-text'] or
                [c['repeats'] for c in cases] != [3]*5 + [1]*8 or
                [c['role'] for c in cases] != ['held_out_acceptance']*9 +
                ['development_regression']*4):
            raise ValueError('case order/repetition budget changed')
    timing = [s for s in steps if s['kind']=='timing']
    if ([(s['head'],s['suffix']) for s in timing] !=
            [('full',False),('full',True),('subset',True),('subset',False)] or
            any(s['http_requests']!=2 or [c['id'] for c in s['cases']]!=['old-8k','old-45k'] or
                [c['repeats'] for c in s['cases']]!=[1,1] for s in timing)):
        raise ValueError('frozen diagnostic request budget/order changed')
    if len({s['name'] for s in steps}) != len(steps):
        raise ValueError('duplicate step identity')


def run_cases(plan, step, output, server, env, capacity, *, client_check=None):
    results = []
    for item in step['cases']:
        case = output / item['id']
        case.mkdir()
        inputs = case / 'requests.jsonl'
        first = Path(item['path']).read_text().splitlines()[0]
        inputs.write_text((first + '\n') * item['repeats'])
        diagnostic = step['kind'] == 'timing'
        save(case / 'case.json', dict(item, block=step.get('block'),
             diagnostic_only=diagnostic, performance_acceptance_sample=(
                 not diagnostic and item['role']=='held_out_acceptance')))
        checked = {} if client_check is None else {'process_check': client_check}
        rows = http_runner.run_case(output, case, inputs, item['repeats'],
            item['input_min'], item['input_max'], item['output_tokens'], True,
            ROOT / 'tools/evalscope/.venv/bin/evalscope', Path(plan['model']),
            plan['port'], env, server, False, capacity, 'performance', **checked)
        if any(row['prompt_sha256'] != item['prompt_sha256'] or
               row['actual_input'] != item['input_tokens'] for row in rows):
            raise RuntimeError('fixture prompt/token identity changed: '+item['id'])
        result = dict(id=item['id'], role=item['role'], block=step.get('block'),
            head=step['head'], suffix=step['suffix'], diagnostic_only=diagnostic,
            source=item, responses=rows,
            outputs=[hashlib.sha256(r['text'].encode()).hexdigest() for r in rows],
            metrics=[dict(ttft=r['ttft'],latency=r['latency'],
                          decode_tps=(r['actual_output']-1)/(r['latency']-r['ttft']),
                          total_tps=r['actual_output']/r['latency']) for r in rows])
        results.append(result)
        save(output / 'results.json', results)
        http_runner.event(output, 'case_completed', case=item['id'],
                          role=item['role'], requests=item['repeats'])
    return results


def custom_step(plan, name):
    legacy.require_guard()
    step = next(s for s in plan['steps'] if s['name']==name)
    if step['kind'] not in ('block','timing','lifecycle'):
        raise ValueError('not a custom admission-stage step')
    output = Path(plan['output']) / name
    output.mkdir(parents=True, exist_ok=False)
    env = legacy.clean_env()
    if step['kind']=='timing': env['Q4T_MTP_TIMING']='1'
    command = [plan['candidate'],'serve','--model-dir',plan['model'],'--port',str(plan['port']),
               '--max-seq','1','--max-prefill','8192','--max-len','208896','--max-tokens','256',
               '--mtp','--mtp-k','3']
    if step['head']=='subset': command += ['--mtp-subset-head',plan['artifact']]
    if step['suffix']: command += ['--mtp-suffix']
    save(output/'server-command.json',dict(argv=command,
         environment={k:v for k,v in env.items() if k.startswith('Q4T_')},
         kind=step['kind'], block=step.get('block'),
         diagnostic_only=step['kind']=='timing',
         performance_acceptance_sample=step['kind']=='block',performance_acceptance=False))
    save(output/'step.json',step)
    passed, failure, server = False, None, None
    with (output/'server.log').open('x') as log:
        try:
            http_runner.event(output,'server_before_start')
            server = subprocess.Popen(command,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT)
            (output/'server.pid').write_text(str(server.pid)+'\n')
            http_runner.event(output,'server_started',pid=server.pid)
            deadline = time.monotonic()+900
            while time.monotonic()<deadline:
                if server.poll() is not None: raise RuntimeError('server exited during startup')
                if 'serving on port' in (output/'server.log').read_text(): break
                time.sleep(1)
            else: raise TimeoutError('server startup exceeded 900 seconds')
            capacity = http_runner.capacity_evidence((output/'server.log').read_text(),208896)
            save(output/'capacity.json',capacity)
            http_runner.event(output,'server_ready',pid=server.pid,capacity=capacity)
            if not capacity['matches_requested']: raise RuntimeError('effective capacity mismatch')
            if step['kind']=='lifecycle':
                # Controlled old material: lifecycle evidence is not held-out throughput.
                results = lifecycle_admission(plan,step,output)
            else:
                results = run_cases(plan,step,output,server,env,capacity)
            save(output/'results.json',results)
            passed = True
        except BaseException as error:
            failure = repr(error)
            raise
        finally:
            if server is not None:
                http_runner.event(output,'server_before_shutdown',pid=server.pid)
                if server.poll() is None:
                    server.terminate()
                    try: server.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait(timeout=10)
                http_runner.event(output,'server_after_shutdown',pid=server.pid,
                                  server_returncode=server.returncode)
            clean = passed and server is not None and server.returncode==0
            save(output/'exit.json',dict(server=server.returncode if server else None,
                 http_output_checks_passed=clean,failure=failure,
                 diagnostic_only=step['kind']=='timing',
                 performance_acceptance_sample=step['kind']=='block',performance_acceptance=False))
    if not clean: raise RuntimeError('custom HTTP group did not complete cleanly')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan',type=Path,required=True)
    parser.add_argument('--custom-step')
    args = parser.parse_args()
    plan_path = args.plan.resolve()
    plan = json.loads(plan_path.read_text())
    validate_plan(plan)
    if args.custom_step:
        custom_step(plan,args.custom_step)
    else:
        # Local adapter only; no frozen source file is changed. The reused outer
        # loop calls this audit after each HTTP step before allowing the next.
        legacy.audit_server = audit_server
        legacy.run_stage(plan,plan_path)


if __name__=='__main__':
    main()

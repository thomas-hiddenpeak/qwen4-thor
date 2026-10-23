"""One bounded numeric-admission stage beneath the shared outer GPU lease.

The original suffix driver stays unchanged. Its resource/process lifecycle is
reused with a stricter audit callback; custom HTTP still uses evalscope.run_case.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
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
    """Reject missing admission/table evidence, fallback, or overlapping costs."""
    _BASE_AUDIT(output, expected_requests)
    text = (output / 'server.log').read_text()
    for marker in ['MTP trunk alloc failed', 'plain decode for this request',
                   'MTP load failed', 'MTP init failed', 'MTP allocation failed']:
        if marker in text:
            raise RuntimeError('MTP fallback invalidates this arm: ' + marker)
    argv = json.loads((output / 'server-command.json').read_text())['argv']
    enabled = '--mtp-suffix' in argv
    table = [dict(re.findall(r'(\w+)=(\S+)', line)) for line in text.splitlines()
             if line.startswith('[suffix-classes] ')]
    summaries = json.loads((output / 'server-audit.json').read_text())['suffix']
    if not enabled:
        if table:
            raise RuntimeError('suffix-off arm initialized the classification table')
        return
    init = [r for r in table if r.get('event') == 'init']
    release = [r for r in table if r.get('event') == 'release']
    enabled_lines = [line for line in text.splitlines() if 'suffix enabled:' in line]
    if (len(init) != 1 or len(release) != 1 or len(table) != 2 or
            len(enabled_lines) != 1 or 'numeric_gate=1' not in enabled_lines[0]):
        raise RuntimeError('missing/duplicate numeric-gate table lifecycle; old binary is not eligible')
    try:
        if (int(init[0]['bytes']) != 248320 or int(init[0]['cap_bytes']) != 262144 or
                int(init[0]['unknown']) != 243 or int(init[0]['init_ns']) < 0 or
                int(release[0]['bytes']) != 248320 or int(release[0]['release_ns']) < 0):
            raise ValueError('table size/capacity/decoder domain mismatch')
        for row in summaries:
            values = {key: int(row[key]) for key in
                      ['queries','hits','peak_bytes','query_ns','numeric_checks',
                       'classified_tokens','numeric_rejected','numeric_ns']}
            if any(v < 0 for v in values.values()) or not 0 < values['peak_bytes'] <= 2*1024*1024:
                raise ValueError('invalid suffix resource/counter value')
            checks = values['numeric_checks']
            if (checks != values['hits'] + values['numeric_rejected'] or
                    checks > values['queries'] or
                    not 3*checks <= values['classified_tokens'] <= 64*checks or
                    values['numeric_ns'] > values['query_ns']):
                raise ValueError('numeric admission counts or nested timing are inconsistent')
    except (KeyError, ValueError) as error:
        raise RuntimeError('numeric-gate evidence invalid: ' + str(error)) from error
    save(output / 'numeric-audit.json', dict(table=table, requests=summaries,
         cost_scope='numeric_ns is included in query_ns; never add both',
         table_bytes=248320, table_cap_bytes=262144, request_index_cap_bytes=2*1024*1024))


def validate_plan(plan):
    steps = plan['steps']
    if (plan['http_requests'] != 238 or plan['http_loads'] != 18 or
            plan['stage_timeout_seconds'] != 14400 or
            plan['direct_contract_timeout_seconds'] != 1200 or
            sum(s['http_requests'] for s in steps) != 238 or
            sum(s['http_loads'] for s in steps) != 18 or
            steps[0]['name'] != 'quality-full-on' or
            steps[0]['kind'] != 'quality' or steps[0]['http_requests'] != 11 or
            steps[0]['head'] != 'full' or steps[0]['suffix'] is not True):
        raise ValueError('plan differs from frozen numeric-stage budget/first HTTP gate')
    blocks = [s for s in steps if s['kind'] == 'block']
    if len(blocks) != 8 or any(s['http_requests'] != 22 for s in blocks):
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
                 'old-8k','old-repeat-code','old-repeat-text'] or
                [c['repeats'] for c in cases] != [3]*5 + [1]*7 or
                [c['role'] for c in cases] != ['held_out_acceptance']*9 +
                ['development_regression']*3):
            raise ValueError('case order/repetition budget changed')
    if len({s['name'] for s in steps}) != len(steps):
        raise ValueError('duplicate step identity')


def run_cases(plan, step, output, server, env, capacity):
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
        rows = http_runner.run_case(output, case, inputs, item['repeats'],
            item['input_min'], item['input_max'], item['output_tokens'], True,
            ROOT / 'tools/evalscope/.venv/bin/evalscope', Path(plan['model']),
            plan['port'], env, server, False, capacity, 'performance')
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
        raise ValueError('not a custom numeric-stage step')
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
         diagnostic_only=step['kind']=='timing', performance_acceptance=False))
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
                old_code = next(c for c in plan['development'] if c['id']=='old-repeat-code')
                lifecycle_plan = dict(plan,scenarios=[dict(old_code,id='repeat-code')])
                results = legacy.lifecycle(lifecycle_plan,step,output)
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
                 diagnostic_only=step['kind']=='timing',performance_acceptance=False))
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

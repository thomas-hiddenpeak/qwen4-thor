"""Bounded heterogeneous agent HTTP stage, using the repaired shared monitor."""
import importlib.util
import json
import math
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('agent_multi', ROOT / 'tools/mtp_suffix_multi/run_stage.py')
multi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(multi)
reuse, legacy, http, lazy = multi.reuse, multi.legacy, multi.http, multi.lazy
save = legacy.save


def validate(plan):
    if plan['stage'] != 'AGENT' or plan['maximum_http_requests'] != 323:
        raise ValueError('frozen agent scope')
    if (plan['maximum_starts'], plan['maximum_gpu_contracts']) != (18, 4):
        raise ValueError('frozen startup/fixture budget')
    if not 0 < plan['stage_timeout_seconds'] <= 28800:
        raise ValueError('wall budget')
    if time.time() >= plan['task_deadline_unix']:
        raise TimeoutError('absolute task deadline')
    services = [s for s in plan['steps'] if s['http_loads']]
    if len(services) > 18 or sum(s['http_requests'] for s in services) != plan['http_requests']:
        raise ValueError('request/start ledger')
    for s in services:
        if s['http_requests'] != sum(c['count'] for c in s['cases']) + 3 * s.get('lifecycle', False):
            raise ValueError('case ledger')
    if plan.get('first_http_reuse'):
        gate = Path(plan['first_http_reuse'])
        if legacy.sha(gate) != plan['first_http_reuse_sha256']:
            raise ValueError('HTTP reuse identity')
        receipt = json.loads(gate.read_text())
        if not receipt['passed'] or receipt['candidate'] != plan['candidate_sha256']:
            raise ValueError('HTTP reuse inference changed')
    elif services[0]['cases'][0]['id'] != 'quality':
        raise ValueError('HTTP first')


def paired_gate(plan):
    """Conditional second block: every head/scenario must first have net gain."""
    results = []
    for head in ['full', 'subset']:
        sides = {}
        for s in plan['steps']:
            if s.get('block') != 1 or s.get('head') != head: continue
            path = Path(plan['output']) / s['name'] / 'results.json'
            sides[s['suffix']] = {r['id']: r for r in json.loads(path.read_text())}
        for name in ['repeat-code', 'repeat-text']:
            a, b = sides[False][name], sides[True][name]
            ratio = b['throughput'] / a['throughput']
            results.append(dict(head=head, scenario=name, e2e_ratio=ratio))
    save(Path(plan['output']) / 'first-block-decision.json', dict(results=results,
         continue_second_block=all(r['e2e_ratio'] > 1 for r in results)))
    if any(r['e2e_ratio'] <= 1 for r in results):
        raise RuntimeError('NO_GO_FIRST_BLOCK_NET_GAIN; no expansion')


def custom_step(plan, name):
    legacy.require_guard()
    step = next(s for s in plan['steps'] if s['name'] == name)
    if step.get('conditional_gate'): paired_gate(plan)
    out = Path(plan['output']) / name
    out.mkdir()
    env = {k: v for k, v in legacy.clean_env().items() if not k.startswith('CUPTI_')}
    explicit = step['layout'] == 'agent'
    slots, prefill = (4, 4096) if explicit else (1, 8192)
    command = [step['binary'], 'serve', '--model-dir', plan['model'], '--host',
        '127.0.0.1', '--port', str(plan['port']), '--max-seq', str(slots),
        '--max-prefill', str(prefill), '--max-len', '208896', '--max-tokens', '256']
    if explicit: command += ['--seq-capacities', '208896,65536,65536,65536']
    command += ['--no-mtp'] if step['head'] == 'plain' else ['--mtp', '--mtp-k', '3']
    if step['head'] == 'subset': command += ['--mtp-subset-head', plan['artifact']]
    if step['suffix']: command += ['--mtp-suffix']
    save(out / 'server-command.json', dict(argv=command, environment={
        k:v for k,v in env.items() if k.startswith('Q4T_')}))
    save(out / 'step.json', step)
    server, failure, passed = None, None, False
    deadline = time.monotonic() + step['timeout'] - 45
    def check():
        if time.monotonic() >= deadline or time.time() >= plan['task_deadline_unix']:
            raise TimeoutError('bounded agent HTTP step')
        if server is not None and server.poll() is not None:
            raise RuntimeError('server exited early')
    with (out / 'server.log').open('x') as log:
        try:
            http.event(out, 'server_before_start')
            server = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
            (out / 'server.pid').write_text(str(server.pid) + '\n')
            http.event(out, 'server_started', pid=server.pid)
            startup = min(deadline, time.monotonic() + 900)
            while time.monotonic() < startup:
                check()
                if 'serving on port' in (out / 'server.log').read_text(): break
                time.sleep(1)
            else: raise TimeoutError('agent startup')
            raw = (out / 'server.log').read_text()
            lines = re.findall(r'\[q4t\]\[capacity\] ([^\n]+)', raw)
            if len(lines) != 1: raise RuntimeError('capacity receipt absent')
            capacity = dict(re.findall(r'(\w+)=(\S+)', lines[0]))
            for prefix in ['requested', 'effective']:
                for key, value in [('max_seq', slots), ('max_len', 208896), ('max_prefill', prefill)]:
                    if int(capacity[prefix + '_' + key]) != value:
                        raise RuntimeError('capacity silently reduced')
            if explicit and 'capacities=208896,65536,65536,65536 stream_mtp_prefill=1 text_admission=32' not in raw:
                raise RuntimeError('explicit slot/streaming receipt absent')
            save(out / 'capacity.json', capacity)
            http.event(out, 'server_ready', pid=server.pid, capacity=capacity)
            results = []
            for c in step['cases']:
                result = multi.case(plan, step, c, out, env, check)
                if c.get('prompts'):
                    actual = sorted((r['prompt_sha256'], r['input_tokens']) for r in result['rows'])
                    expected = sorted((r['sha256'], r['tokens']) for r in c['prompts'])
                    if actual != expected: raise RuntimeError('exact workload/input coverage')
                results.append(result)
                save(out / 'results.json', results)
                if step == plan['steps'][0] and c['id'] == 'quality':
                    save(Path(plan['output']) / 'first-http-gate.json',
                         dict(passed=True, requests=c['count'], candidate=plan['candidate_sha256'], unix=time.time()))
            if step.get('lifecycle'):
                save(out / 'lifecycle-results.json', lazy.admission.lifecycle_admission(plan, step, out, expected_slots=slots))
            check()
            passed = True
        except BaseException as error:
            failure = repr(error)
            raise
        finally:
            if server is not None:
                http.event(out, 'server_before_shutdown', pid=server.pid)
                if server.poll() is None:
                    server.terminate()
                    try: server.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        server.kill(); server.wait(timeout=10)
                http.event(out, 'server_after_shutdown', pid=server.pid, server_returncode=server.returncode)
            save(out / 'exit.json', dict(server=server.returncode if server else None,
                 http_output_checks_passed=passed and server is not None and server.returncode == 0,
                 failure=failure))
    if not passed or server.returncode: raise RuntimeError('unclean agent service')


def audit_server(out, count):
    lazy.admission._BASE_AUDIT(out, count)
    step = json.loads((out / 'step.json').read_text())
    raw = (out / 'server.log').read_text()
    fields = lambda line: dict(re.findall(r'(\w+)=(\S+)', line))
    admitted = [fields(l) for l in raw.splitlines() if 'event=admitted ' in l]
    ended = [fields(l) for l in raw.splitlines() if 'event=end ' in l]
    if (len(ended) != count or {r['id'] for r in admitted} != {r['id'] for r in ended}
        or any(r.get('released') != '1' or r.get('outcome') not in ['success','cancelled'] for r in ended)):
        raise RuntimeError('request release/outcome')
    suffix = [fields(l) for l in raw.splitlines() if l.startswith('[suffix] ')]
    for r in suffix: lazy.audit_stats(r)
    tables = [fields(l) for l in raw.splitlines() if l.startswith('[suffix-classes] ')]
    if suffix:
        if len(tables) != 2 or [r['event'] for r in tables] != ['configure','release']:
            raise RuntimeError('class cache lifecycle')
        for key, local in [('decodes','cache_decodes'), ('lookups','cache_lookups'),
                           ('allocations','cache_allocations'), ('checks','numeric_checks')]:
            if int(tables[-1][key]) != sum(int(r[local]) for r in suffix):
                raise RuntimeError('cache accounting: ' + key)
    elif tables: raise RuntimeError('cache while suffix off')
    batches = [{k:int(v) for k,v in fields(l).items()} for l in raw.splitlines()
               if l.startswith('[mtp-batches] ')]
    hist = [fields(l) for l in raw.splitlines() if l.startswith('[mtp-accept] ')]
    if step['head'] != 'plain':
        if len(hist) != count or sum(sum(map(int,r['draft_counts'].split(','))) for r in hist) != sum(r['steps'] * r['rows'] for r in batches):
            raise RuntimeError('verify/acceptance work ledger')
        if sum(r['steps'] * r['suffix'] for r in batches) != sum(int(r['hits']) for r in suffix):
            raise RuntimeError('suffix/verify work ledger')
    if step.get('block'):
        if not batches or max(r['rows'] for r in batches) != 4:
            raise RuntimeError('four active MTP rows not exercised')
        if suffix and not any(0 < r['suffix'] < r['rows'] for r in batches):
            raise RuntimeError('mixed neural/suffix rows not exercised')
    streams = [fields(l) for l in raw.splitlines() if l.startswith('[mtp-prefill] ')]
    if step['layout'] == 'agent':
        if len(streams) != count or any(r['ready'] != '1' or int(r['trunk_bytes']) > 4096 * 10240 * 2 for r in streams):
            raise RuntimeError('chunk trunk lifetime/readiness')
        caps = [fields(l) for l in raw.splitlines() if l.startswith('[q4t][slot-admission] ')]
        if len(caps) != count or any(int(r['required']) > int(r['capacity']) for r in caps):
            raise RuntimeError('requested output exceeds admitted slot')
    save(out / 'agent-audit.json', dict(passed=True, lifecycle=ended, suffix=suffix,
          batches=batches, acceptance=hist, cache=tables, streamed_prefill=streams))


if __name__ == '__main__':
    def interrupted(*_): raise KeyboardInterrupt('owned agent stage interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    reuse.validate = validate
    reuse.custom_step = custom_step
    reuse.admission.audit_server = audit_server
    reuse.main()

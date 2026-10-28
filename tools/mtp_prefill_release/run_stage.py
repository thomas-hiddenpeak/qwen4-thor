"""Fixed release phases; inference program and existing monitor are unchanged."""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


seed = module('release_seed', 'tools/prefix_seed/run_stage.py')
agent = module('release_agent', 'tools/mtp_suffix_agent/run_stage.py')
reuse, legacy, http, multi, save = (seed.reuse, seed.legacy, seed.http,
                                   seed.multi, seed.save)
request, cached, capability, turn = (seed.request, seed.cached,
                                   seed.capability, seed.turn)


def validate(plan):
    budgets = {'A': (112, 6), 'B': (24, 2), 'S': (32, 4),
               'C': (128, 8), 'H': (6, 2)}
    phase = plan['stage'].removeprefix('PREFILL_RELEASE_')
    if phase not in budgets or (plan['http_requests'], plan['http_loads']) != budgets[phase]:
        raise ValueError('frozen release budget')
    if (plan['maximum_http_requests'], plan['maximum_starts'],
            plan['maximum_gpu_contracts']) != (302, 22, 0):
        raise ValueError('total release budget')
    if (sum(s['http_requests'] for s in plan['steps']) != plan['http_requests'] or
            sum(s['http_loads'] for s in plan['steps']) != plan['http_loads']):
        raise ValueError('step ledger')
    if time.time() >= plan['task_deadline_unix']:
        raise TimeoutError('absolute release deadline')
    for path in plan.get('required_gates', []):
        gate = json.loads(Path(path).read_text())
        if not gate['passed'] or gate['candidate_sha256'] != plan['candidate_sha256']:
            raise ValueError('release prerequisite: ' + path)


def lifecycle(plan, out, check):
    """The delivered 12-request lifecycle, without unchanged parser probes."""
    prompt = Path(plan['development_prompt']).read_text()
    scope, child = capability('release-cache-scope'), capability('release-cache-child')
    question = 'What is the audit_code in the authoritative shared record? Return only its six digits.'
    cold = request(plan, out, 'readonly-cold', prompt, 8, {'prefix_cache_scope': scope})
    again = request(plan, out, 'readonly-again', prompt+cold['text']+turn(question),
                    16, {'prefix_cache_scope': scope})
    if cached(cold) or cached(again) or again['text'].strip() != '831907':
        raise RuntimeError('readonly miss/recall')
    source = request(plan, out, 'source', prompt, 8,
                     {'prefix_cache_scope': scope, 'prefix_cache_write_tokens': 8192})
    request(plan, out, 'overwrite', plan['short_prompt'], 16)
    branch = prompt+source['text']+turn(question)
    hit = request(plan, out, 'child', branch, 16,
                  {'prefix_cache_scope': scope, 'session_id': child})
    fresh = request(plan, out, 'fresh', branch, 16)
    isolated = request(plan, out, 'isolated', branch, 16,
                       {'prefix_cache_scope': capability('other-scope')})
    mismatch = request(plan, out, 'mismatch', branch.replace('archive', 'record', 1),
                       16, {'prefix_cache_scope': scope})
    if (cached(source) or source['usage']['completion_tokens'] != 8 or
            cached(hit) != 8192 or cached(isolated) or cached(mismatch)):
        raise RuntimeError('shared selection')
    if any(r['text'].strip() != '831907' for r in [hit, fresh, isolated, mismatch]):
        raise RuntimeError('shared/fresh exact quality')
    resumed = request(plan, out, 'session-priority',
        branch+hit['text']+turn('Reply with exactly 44.'), 16,
        {'prefix_cache_scope': scope, 'prefix_cache_write_tokens': 8192,
         'session_id': child})
    if cached(resumed) <= 8192 or resumed['text'].strip() != '44':
        raise RuntimeError('longer session priority')
    duplicate = request(plan, out, 'duplicate-seed', prompt, 8,
        {'prefix_cache_scope': scope, 'prefix_cache_write_tokens': 8192})
    if cached(duplicate) != 8192:
        raise RuntimeError('duplicate seed read')
    cancel_scope = capability('release-cancel-scope')
    cancelled = request(plan, out, 'cancel', prompt, 256,
        {'prefix_cache_scope': cancel_scope, 'prefix_cache_write_tokens': 8192}, True)
    deadline = time.monotonic()+30
    while time.monotonic() < deadline:
        check()
        with urllib.request.urlopen(f"http://127.0.0.1:{plan['port']}/healthz", timeout=5) as response:
            health = json.load(response)
        if health['seq_slots_free'] == 4 and health['gpu_healthy']:
            break
        time.sleep(.1)
    else:
        raise TimeoutError('cancel slot release')
    after = request(plan, out, 'after-cancel', branch, 16,
                    {'prefix_cache_scope': cancel_scope})
    if cached(after) or after['text'].strip() != '831907':
        raise RuntimeError('cancel publication')
    def rid(row):
        return next(m['id'] for m in row['messages'] if m.get('id'))
    writes = [dict(re.findall(r'(\w+)=(\S+)', line))
              for line in (out/'server.log').read_text().splitlines()
              if line.startswith('[prefix-cache] request=')]
    if (len(writes) != 2 or {r['request'] for r in writes} != {rid(source), rid(cancelled)} or
            any(r['prefix'] != '8192' or r['error'] != 'none' for r in writes)):
        raise RuntimeError('seed write policy')
    save(out/'lifecycle.json', dict(passed=True, requests=12,
        shared_tokens=cached(hit), session_tokens=cached(resumed),
        seed_writes=writes, cancel_unpublished=True, readonly_zero_writes=True))


def health_case(plan, step, out, check):
    prompt = Path(plan['development_prompt']).read_text()
    scope = capability('installed-scoring-'+step['head'])
    parent = capability('installed-scoring-parent')
    first = request(plan, out, 'source', prompt, 8,
        dict(prefix_cache_scope=scope, prefix_cache_write_tokens=8192, session_id=parent))
    second = request(plan, out, 'child', prompt+first['text']+turn(
        'What is the audit_code in the authoritative shared record? Return only its six digits.'),
        16, dict(prefix_cache_scope=scope, session_id=capability('installed-scoring-child')))
    third = request(plan, out, 'parent-resume', prompt+first['text']+turn('Reply with exactly 44.'),
        16, dict(prefix_cache_scope=scope, session_id=parent))
    if (cached(first) or cached(second) != 8192 or second['text'].strip() != '831907' or
            cached(third) <= 8192 or third['text'].strip() != '44'):
        raise RuntimeError('installed shared/private restore')
    writes = [line for line in (out/'server.log').read_text().splitlines()
              if line.startswith('[prefix-cache] request=')]
    if len(writes) != 1 or 'prefix=8192 ' not in writes[0] or 'error=none' not in writes[0]:
        raise RuntimeError('installed single seed')
    check()
    save(out/'health-result.json', dict(passed=True, requests=3, head=step['head'],
        first=first, second=second, third=third, writes=writes))


def custom_step(plan, name):
    legacy.require_guard()
    step = next(s for s in plan['steps'] if s['name'] == name)
    if step['kind'] == 'analysis':
        module('release_analysis', 'tools/mtp_prefill_release/analyze.py').first_c_block(plan)
        return
    phase = plan['stage'].removeprefix('PREFILL_RELEASE_')
    out = Path(plan['output'])/name
    out.mkdir()
    command = [step['binary'], 'serve', '--model-dir', plan['model'], '--host', '127.0.0.1',
        '--port', str(plan['port']), '--max-seq', '4', '--max-prefill', '4096',
        '--max-len', '208896', '--seq-capacities', '208896,65536,65536,65536']
    if step['head'] == 'plain':
        command += ['--no-mtp']
    else:
        if step['suffix']:
            command += ['--mtp-suffix']
        for kind in ['session', 'prefix']:
            cache = out/kind
            cache.mkdir(mode=0o700)
            command += ['--'+kind+'-cache-dir', str(cache)]
        if step['head'] == 'subset':
            command += ['--mtp-subset-head', plan['artifact']]
    env = {k: v for k, v in legacy.clean_env().items() if not k.startswith('CUPTI_')}
    save(out/'server-command.json', dict(argv=command, environment={
        k: v for k, v in env.items() if k.startswith('Q4T_')}))
    save(out/'step.json', step)
    deadline = min(time.time()+step['timeout']-40, plan['task_deadline_unix'])
    server, failure, passed = None, None, False
    def check():
        if time.time() >= deadline:
            raise TimeoutError('bounded step deadline')
        if server is not None and server.poll() is not None:
            raise RuntimeError('server exited')
    with (out/'server.log').open('x') as log:
        try:
            http.event(out, 'server_before_start')
            server = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
            http.event(out, 'server_started', pid=server.pid)
            while 'serving on port' not in (out/'server.log').read_text():
                check()
                time.sleep(1)
            raw = (out/'server.log').read_text()
            if 'capacities=208896,65536,65536,65536' not in raw:
                raise RuntimeError('four-slot capacity')
            capacity = re.findall(r'\[q4t\]\[capacity\] ([^\n]+)', raw)
            if len(capacity) != 1:
                raise RuntimeError('capacity receipt')
            fields = dict(re.findall(r'(\w+)=(\S+)', capacity[0]))
            for prefix in ['requested', 'effective']:
                for key, value in [('max_seq', 4), ('max_len', 208896), ('max_prefill', 4096)]:
                    if int(fields[prefix+'_'+key]) != value:
                        raise RuntimeError('capacity reduced')
            if step['head'] != 'plain' and any('['+kind+'-cache] enabled=1' not in raw
                                              for kind in ['session', 'prefix']):
                raise RuntimeError('cache disabled')
            http.event(out, 'server_ready', pid=server.pid)
            if phase in ['A', 'S']:
                results = []
                for case in step['cases']:
                    result = multi.case(plan, step, case, out, env, check)
                    results.append(result)
                    save(out/'results.json', results)
                    if phase == 'A' and case['id'] != 'quality':
                        for row in result['rows']:
                            ref = plan['references'][step['head']][case['id']]
                            if any(row[k] != ref[k] for k in ['prompt_sha256', 'input_tokens', 'output_sha256']):
                                raise RuntimeError('frozen five-tier reference')
                    if phase == 'S':
                        expected = sorted((p['sha256'], p['tokens']) for p in case['prompts'])
                        actual = sorted((r['prompt_sha256'], r['input_tokens']) for r in result['rows'])
                        if actual != expected:
                            raise RuntimeError('four-agent input contract')
                raw = (out/'server.log').read_text()
                if '[session-cache] request=' in raw or '[prefix-cache] request=' in raw:
                    raise RuntimeError('uncached control performed cache IO')
            elif phase == 'B':
                lifecycle(plan, out, check)
            elif phase == 'C':
                seed.performance(plan, step, out, env, check)
            elif phase == 'H':
                health_case(plan, step, out, check)
            else:
                raise ValueError('phase')
            if '[prefill-score-timing]' in (out/'server.log').read_text():
                raise RuntimeError('diagnostic in performance run')
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
                    try:
                        server.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait(timeout=10)
                http.event(out, 'server_after_shutdown', pid=server.pid, server_returncode=server.returncode)
            save(out/'exit.json', dict(server=server.returncode if server else None,
                http_output_checks_passed=passed and server is not None and server.returncode == 0,
                failure=failure))


def audit(out, count):
    step = json.loads((out/'step.json').read_text())
    if step.get('suffix_performance'):
        agent.audit_server(out, count)
    else:
        seed.audit(out, count)


if __name__ == '__main__':
    reuse.validate = validate
    reuse.custom_step = custom_step
    reuse.admission.audit_server = audit
    parser = argparse.ArgumentParser()
    parser.add_argument('--plan', required=True, type=Path)
    parser.add_argument('--custom-step')
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    validate(plan)
    preparation = None
    if plan['stage'] == 'PREFILL_RELEASE_C' and not args.custom_step:
        legacy.require_guard()
        legacy.verify_files(plan)
        io = module('release_io', 'tools/prefix_seed/precondition.py')
        preparation = io.prepare(args.plan.parent, plan['task_deadline_unix'])
    try:
        reuse.main()
    finally:
        if preparation is not None:
            io.release(preparation, args.plan.parent)

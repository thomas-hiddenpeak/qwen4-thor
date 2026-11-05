"""Bounded mainline scoring acceptance; existing evalscope and lease monitor."""
import importlib.util
import json
from pathlib import Path
import re
import signal
import statistics
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


seed = module('scoring_seed', 'tools/prefix_seed/run_stage.py')
reuse, legacy, http, multi = seed.reuse, seed.legacy, seed.http, seed.multi
metrics = module('scoring_metrics', 'tools/mtp_suffix_agent/analyze.py')
save = legacy.save


def verify_reuse(plan):
    record = json.loads(Path(plan['reuse_record']).read_text())
    if (record['completed_requests'] != 26 or record['original_admitted'] != 34
            or record['candidate_sha256'] != plan['candidate_sha256']
            or record['absolute_deadline_unchanged'] != plan['task_deadline_unix']):
        raise ValueError('partial evidence identity or budget changed')
    for key in ('original_plan', 'original_raw_seal'):
        item = record[key]
        if legacy.sha(Path(item['path'])) != item['sha256']:
            raise ValueError('original evidence changed: ' + key)
    old = json.loads(Path(record['original_plan']['path']).read_text())
    for name, digest in old['files'].items():
        snapshot = record['original_tools'].get(name, {}).get('path', name)
        if legacy.sha(Path(snapshot)) != digest:
            raise ValueError('original dependency changed: ' + name)
    seal = Path(record['original_raw_seal']['path'])
    for name, digest in json.loads(seal.read_text()).items():
        if legacy.sha(seal.parent / name) != digest:
            raise ValueError('partial raw evidence changed: ' + name)
    for case in record['cases']:
        path = Path(case['reused_from'])
        if legacy.sha(path) != case['reused_sha256']:
            raise ValueError('completed case changed')
        expected = {k: v for k, v in case.items() if not k.startswith('reused_')}
        if json.loads(path.read_text()) != expected:
            raise ValueError('completed case content changed')
    return record


def validate(plan):
    if plan['stage'] != 'SCORING_ADOPTION':
        raise ValueError('unexpected stage')
    requests = 172 if plan.get('reuse_record') else 198
    if (plan['http_requests'], plan['http_loads'],
            plan['maximum_gpu_contracts']) != (requests, 10, 2):
        raise ValueError('frozen budget')
    if sum(s['http_requests'] for s in plan['steps']) != requests:
        raise ValueError('request ledger')
    if sum(s['http_loads'] for s in plan['steps']) != 10:
        raise ValueError('service ledger')
    if sum(s['kind'] == 'contract' for s in plan['steps']) != 2:
        raise ValueError('direct contract ledger')
    if time.time() >= plan['task_deadline_unix']:
        raise TimeoutError('frozen absolute deadline')
    if not 0 < plan['stage_timeout_seconds'] <= 10800:
        raise ValueError('three-hour stage budget')
    for s in plan['steps']:
        if s['kind'] == 'matrix' and (
                sum(c['count'] for c in s['cases']) != s['http_requests']):
            raise ValueError('case budget')
        for case in s.get('cases', []):
            if case['id'].startswith('repeat-') and case['tokens'] != 256:
                raise ValueError('repeat output budget must be explicit')
    if plan.get('reuse_record'):
        record = verify_reuse(plan)
        first = plan['steps'][0]
        if (first['name'] != '00-agent-full-candidate' or
                first['http_requests'] != 8 or
                {c['id'] for c in first['cases']} != {'repeat-code', 'repeat-text'} or
                sum(len(c['rows']) for c in record['cases']) != 26):
            raise ValueError('continuation scope')


def paired(plan, mode):
    arms = {}
    for s in plan['steps']:
        if s.get('mode') == mode and s['kind'] == 'matrix':
            folder = Path(plan['output']) / s['name']
            ended = json.loads((folder / 'exit.json').read_text())
            if not ended['http_output_checks_passed'] or ended['server'] != 0:
                raise RuntimeError('incomplete paired service')
            arms[s['arm']] = {c['id']: c for c in
                             json.loads((folder / 'results.json').read_text())}
    if set(arms) != {'parent', 'candidate'}:
        raise ValueError('incomplete parent/candidate pair')
    rows = []
    for name, parent in arms['parent'].items():
        candidate = arms['candidate'][name]
        row = dict(mode=mode, case=name, **metrics.compare(parent, candidate))
        def identities(case):
            return sorted((r['prompt_sha256'], r['input_tokens'],
                           r['output_sha256'], r['output_tokens'], r['finish'])
                          for r in case['rows'])
        row['output_same'] = identities(parent) == identities(candidate)
        for side, value in [('parent', parent), ('candidate', candidate)]:
            row[side + '_ttft'] = [r['ttft'] for r in value['rows']]
            row[side + '_latency'] = [r['latency'] for r in value['rows']]
        row['target_met'] = True
        if mode.startswith('agent') and name in (
                'context-204800', 'context-45056'):
            a, b = [statistics.median(row[k + '_ttft'])
                    for k in ('parent', 'candidate')]
            e0, e1 = [statistics.median(row[k + '_latency'])
                      for k in ('parent', 'candidate')]
            row['median_ttft_reduction'] = 1 - b / a
            row['target_met'] = (1 - b / a >= .2 if name.endswith('204800')
                                 else b < a) and e1 < e0
        rows.append(row)
    result = dict(mode=mode, passed=all(r['output_same'] and r['target_met']
                                      for r in rows), results=rows,
                  statistics='DESCRIPTIVE_NOT_NONINFERIORITY',
                  protection_review='REQUIRED_NO_AUTOMATIC_TOLERANCE')
    result['negative_observations'] = [r['case'] for r in rows
        if r['ttft_percent'] > 0 or r['decode_percent'] < 0 or
        r['e2e_percent'] < 0]
    return result


def lifecycle(plan, out, check):
    # Preserve the delivered seed lifecycle; exclude unrelated rejection probes.
    original = seed.rejected_probes
    seed.rejected_probes = lambda *_: None
    try:
        seed.lifecycle(plan, out, check)
    finally:
        seed.rejected_probes = original
    result = json.loads((out / 'lifecycle.json').read_text())
    result['rejected_http'] = 0
    save(out / 'lifecycle.json', result)


def custom_step(plan, name):
    legacy.require_guard()
    step = next(s for s in plan['steps'] if s['name'] == name)
    out = Path(plan['output']) / name
    out.mkdir()
    save(out / 'step.json', step)
    if step['kind'] == 'gate':
        result = paired(plan, step['mode'])
        save(out / 'gate.json', result)
        if not result['passed']:
            raise RuntimeError('NO_GO output or frozen target: ' + step['mode'])
        return
    env = {k: v for k, v in legacy.clean_env().items()
           if not k.startswith('CUPTI_')}
    if step['kind'] == 'contract':
        command = [plan['state_contract'], plan['model'], str(out / 'cache')]
        if step['head'] == 'subset':
            command.append(plan['artifact'])
        save(out / 'command.json', command)
        with (out / 'contract.log').open('x') as log:
            result = subprocess.run(command, cwd=ROOT, env=env, stdout=log,
                stderr=subprocess.STDOUT, timeout=min(600,
                    plan['task_deadline_unix'] - time.time()))
        raw = (out / 'contract.log').read_text()
        passed = (result.returncode == 0 and
                  raw.count('PASS scoring_reference ') == 4 and
                  raw.count('PASS prefix_state=1 ') == 1 and 'FAIL ' not in raw)
        save(out / 'result.json', dict(passed=passed,
            returncode=result.returncode, head=step['head']))
        if not passed:
            raise RuntimeError('independent scoring/state contract failed')
        return
    # Only the installed evalscope executable comes from the original checkout;
    # all imported driver code and inference sources come from this worktree.
    multi.ROOT = Path(plan['execution_tools_root'])
    single = step['layout'] == 'single'
    slots, prefill = (1, 8192) if single else (4, 4096)
    command = [step['binary'], 'serve', '--model-dir', plan['model'],
        '--host', '127.0.0.1', '--port', str(plan['port']), '--max-seq',
        str(slots), '--max-prefill', str(prefill), '--max-len', '208896',
        '--max-tokens', '256']
    if not single:
        command += ['--seq-capacities', '208896,65536,65536,65536']
    command += (['--no-mtp'] if step['head'] == 'plain'
                else ['--mtp', '--mtp-k', '3'])
    if step['head'] == 'subset':
        command += ['--mtp-subset-head', plan['artifact']]
    if step['suffix']:
        command += ['--mtp-suffix']
    if not single:
        for kind in ['session', 'prefix']:
            cache = out / kind
            cache.mkdir(mode=0o700)
            command += ['--' + kind + '-cache-dir', str(cache)]
    save(out / 'server-command.json', dict(argv=command,
        environment={k: v for k, v in env.items() if k.startswith('Q4T_')}))
    deadline = min(time.monotonic() + step['timeout'] - 40,
        time.monotonic() + plan['task_deadline_unix'] - time.time() - 40)
    server, passed, failure = None, False, None

    def check():
        if time.monotonic() >= deadline:
            raise TimeoutError('bounded service deadline')
        if server is not None and server.poll() is not None:
            raise RuntimeError('server exited early')

    with (out / 'server.log').open('x') as log:
        try:
            http.event(out, 'server_before_start')
            server = subprocess.Popen(command, cwd=ROOT, env=env,
                stdout=log, stderr=subprocess.STDOUT)
            (out / 'server.pid').write_text(str(server.pid) + '\n')
            http.event(out, 'server_started', pid=server.pid)
            startup = min(deadline, time.monotonic() + 300)
            while 'serving on port' not in (out / 'server.log').read_text():
                check()
                if time.monotonic() >= startup:
                    raise TimeoutError('startup deadline')
                time.sleep(.5)
            raw = (out / 'server.log').read_text()
            receipts = re.findall(r'\[q4t\]\[capacity\] ([^\n]+)', raw)
            if len(receipts) != 1:
                raise RuntimeError('missing capacity receipt')
            capacity = dict(re.findall(r'(\w+)=(\S+)', receipts[0]))
            for prefix in ('requested', 'effective'):
                for key, value in [('max_seq', slots), ('max_len', 208896),
                                   ('max_prefill', prefill)]:
                    if int(capacity[prefix + '_' + key]) != value:
                        raise RuntimeError('capacity reduced')
            if not single and 'capacities=208896,65536,65536,65536' not in raw:
                raise RuntimeError('heterogeneous capacity missing')
            save(out / 'capacity.json', capacity)
            http.event(out, 'server_ready', pid=server.pid, capacity=capacity)
            if step['kind'] == 'matrix':
                results = (verify_reuse(plan)['cases'] if plan.get('reuse_record')
                           and name == '00-agent-full-candidate' else [])
                for case in step['cases']:
                    result = multi.case(plan, step, case, out, env, check)
                    if case.get('prompts'):
                        actual = sorted((r['prompt_sha256'], r['input_tokens'])
                                        for r in result['rows'])
                        expected = sorted((r['sha256'], r['tokens'])
                                          for r in case['prompts'])
                        if actual != expected:
                            raise RuntimeError('four-agent frozen inputs')
                    results.append(result)
                    save(out / 'results.json', results)
                raw = (out / 'server.log').read_text()
                if '[session-cache] request=' in raw or '[prefix-cache] request=' in raw:
                    raise RuntimeError('performance arm performed snapshot IO')
            else:
                lifecycle(plan, out, check)
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
                http.event(out, 'server_after_shutdown', pid=server.pid,
                           server_returncode=server.returncode)
            save(out / 'exit.json', dict(server=server.returncode if server else None,
                http_output_checks_passed=passed and server is not None and
                server.returncode == 0, failure=failure))


def audit(out, count):
    seed.audit(out, count)
    step = json.loads((out / 'step.json').read_text())
    if step['kind'] == 'matrix' and step['layout'] == 'agent':
        raw = (out / 'server.log').read_text()
        batches = [dict(re.findall(r'(\w+)=(\S+)', l)) for l in raw.splitlines()
                   if l.startswith('[mtp-batches] ')]
        if not batches or max(int(r['rows']) for r in batches) != 4:
            raise RuntimeError('four active generation rows not exercised')


if __name__ == '__main__':
    def interrupted(*_):
        raise KeyboardInterrupt('owned scoring stage interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    reuse.validate = validate
    reuse.custom_step = custom_step
    reuse.admission.audit_server = audit
    reuse.main()

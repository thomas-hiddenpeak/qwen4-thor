"""Single frozen 90-minute stage; no retries or adaptive request expansion."""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('prefill_seed', ROOT / 'tools/prefix_seed/run_stage.py')
seed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed)
reuse, legacy, http, save = seed.reuse, seed.legacy, seed.http, seed.save


def validate(plan):
    if (plan['stage'], plan['http_requests'], plan['http_loads'],
            plan['stage_timeout_seconds']) != ('PREFILL_SCORE', 16, 8, 5400):
        raise ValueError('frozen stage budget')
    if sum(s['http_requests'] for s in plan['steps']) != 16 or sum(s['http_loads'] for s in plan['steps']) != 8:
        raise ValueError('request/start ledger')
    if sum(s['kind'] == 'contract' for s in plan['steps']) != 2:
        raise ValueError('fixture ledger')
    if time.time() >= plan['task_deadline_unix']:
        raise TimeoutError('absolute deadline')


def benefit_gate(plan):
    results = []
    for head in ['full', 'subset']:
        arms = {}
        for s in plan['steps']:
            if s.get('mode') == 'normal' and s['head'] == head:
                arms[s['arm']] = {r['id']: r for r in json.loads((Path(plan['output']) / s['name'] / 'results.json').read_text())}
        for tier in ['context-8192', 'context-45056', 'context-204800']:
            a, b = arms['parent'][tier]['rows'][0], arms['candidate'][tier]['rows'][0]
            reduction = 1 - b['ttft'] / a['ttft']
            same = all(a[k] == b[k] for k in ['prompt_sha256', 'input_tokens', 'output_sha256', 'output_tokens', 'finish'])
            results.append(dict(head=head, tier=tier, parent=a, candidate=b,
                ttft_reduction=reduction, e2e_reduction=1-b['latency']/a['latency'],
                output_equal=same, target_met=tier != 'context-204800' or (reduction >= .2 and b['latency'] < a['latency'])))
    passed = all(r['output_equal'] and r['target_met'] for r in results)
    save(Path(plan['output']) / 'benefit-gate.json', dict(passed=passed, results=results,
        statistics='DESCRIPTIVE_SINGLE_PAIR', automatic_release=False))
    if not passed:
        raise RuntimeError('NO_GO_BENEFIT_OR_OUTPUT; stop without expansion')


def custom_step(plan, name):
    legacy.require_guard()
    step = next(s for s in plan['steps'] if s['name'] == name)
    if step['kind'] == 'analysis':
        benefit_gate(plan)
        return
    out = Path(plan['output']) / name
    out.mkdir()
    env = legacy.clean_env()
    env.update(step.get('server_env', {}))
    command = [step['binary'], 'serve', '--model-dir', plan['model'], '--host', '127.0.0.1',
        '--port', str(plan['port']), '--max-seq', '4', '--max-prefill', '4096', '--max-len', '208896',
        '--seq-capacities', '208896,65536,65536,65536', '--mtp-suffix']
    for flag, folder in [('session', 'session'), ('prefix', 'prefix')]:
        path = out / folder
        path.mkdir(mode=0o700)
        command += ['--' + flag + '-cache-dir', str(path)]
    if step['head'] == 'subset':
        command += ['--mtp-subset-head', plan['artifact']]
    save(out / 'server-command.json', dict(argv=command, environment={k:v for k,v in env.items() if k.startswith('Q4T_')}))
    save(out / 'step.json', step)
    server, failure, passed = None, None, False
    deadline = min(time.time() + step['timeout'] - 40, plan['task_deadline_unix'])
    def check():
        if time.time() >= deadline: raise TimeoutError('step deadline')
        if server is not None and server.poll() is not None: raise RuntimeError('server exited')
    with (out / 'server.log').open('x') as log:
        try:
            http.event(out, 'server_before_start')
            server = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
            http.event(out, 'server_started', pid=server.pid)
            while 'serving on port' not in (out / 'server.log').read_text():
                check(); time.sleep(1)
            raw = (out / 'server.log').read_text()
            if 'capacities=208896,65536,65536,65536 stream_mtp_prefill=1 text_admission=32' not in raw:
                raise RuntimeError('four-slot capacity changed')
            if '[session-cache] enabled=1' not in raw or '[prefix-cache] enabled=1' not in raw:
                raise RuntimeError('caches not enabled')
            http.event(out, 'server_ready', pid=server.pid)
            results = []
            for case in step['cases']:
                result = seed.multi.case(plan, step, case, out, env, check)
                results.append(result)
                save(out / 'results.json', results)
                r = result['rows'][0]
                if any(r[k] != case[k] for k in ['prompt_sha256', 'input_tokens', 'output_sha256']):
                    raise RuntimeError('frozen input/output reference: ' + case['id'])
            raw = (out / 'server.log').read_text()
            if '[session-cache] request=' in raw or '[prefix-cache] request=' in raw:
                raise RuntimeError('protection unexpectedly used cache')
            timing = [dict(re.findall(r'(\w+)=(\S+)', line)) for line in raw.splitlines()
                      if line.startswith('[prefill-score-timing]')]
            if step['mode'] == 'diagnostic':
                if len(timing) != 200 or any(r.get('ok') != '1' for r in timing):
                    raise RuntimeError('bounded timing incomplete/invalid')
                save(out / 'timing.json', timing)
            elif timing:
                raise RuntimeError('timing enabled in acceptance request')
            check(); passed = True
        except BaseException as error:
            failure = repr(error)
            raise
        finally:
            if server is not None:
                http.event(out, 'server_before_shutdown', pid=server.pid)
                if server.poll() is None:
                    server.terminate()
                    try: server.wait(timeout=30)
                    except subprocess.TimeoutExpired: server.kill(); server.wait(timeout=10)
                http.event(out, 'server_after_shutdown', pid=server.pid, server_returncode=server.returncode)
            save(out / 'exit.json', dict(server=server.returncode if server else None,
                http_output_checks_passed=passed and server is not None and server.returncode == 0, failure=failure))


if __name__ == '__main__':
    reuse.validate = validate
    reuse.custom_step = custom_step
    reuse.admission.audit_server = seed.audit
    reuse.main()

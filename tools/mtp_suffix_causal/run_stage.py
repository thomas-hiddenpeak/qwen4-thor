"""One frozen causal stage; reuse evalscope, shared lease and exit-safe monitor."""
import importlib.util
import json
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('causal_reused_rerun', ROOT / 'tools/mtp_suffix_zero_hit/run_rerun.py')
rerun = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rerun)
legacy, admission, http = rerun.legacy, rerun.admission, rerun.http
save = legacy.save


def fields(line):
    return dict(re.findall(r'(\w+)=(\S+)', line))


def validate(plan):
    if (plan['http_requests'], plan['http_loads'], plan['stage_timeout_seconds']) != (60, 6, 7200):
        raise ValueError('causal budget changed')
    if time.time() >= plan['task_deadline_unix']:
        raise TimeoutError('absolute task deadline reached')
    steps = plan['steps']
    if len(steps) != 6 or [s['suffix'] for s in steps] != [False, True, True, False, True, True]:
        raise ValueError('startup order changed')
    if [len(s['cases']) for s in steps] != [3, 3, 3, 3, 24, 24]:
        raise ValueError('request count changed')
    for s in steps:
        if s['http_loads'] != 1 or s['http_requests'] != len(s['cases']) or s['head'] != 'subset':
            raise ValueError('service shape changed')
        expected = 'suffix-causal-v1 S' + str(int(s['suffix'])) + '\n'
        expected += ''.join(f"{c['input_tokens']} {c['mode']}\n" for c in s['cases'])
        if Path(s['mode_plan']).read_text() != expected:
            raise ValueError('request plan differs from manifest')
        if any(c['repeats'] != 1 or c['output_tokens'] != 256 for c in s['cases']):
            raise ValueError('request shape changed')
    if any(c['mode'] != 'N1' for s in steps[:4] for c in s['cases']):
        raise ValueError('startup-only arm queries index')
    for context in plan['contexts']:
        quartets = {}
        for s in steps[4:]:
            for c in s['cases']:
                if c['context'] == context:
                    quartets.setdefault(c['quartet'], []).append(c['mode'])
        if len(quartets) != 4 or any(sorted(q) != ['N1', 'N2', 'Q', 'R'] for q in quartets.values()):
            raise ValueError('mixed quartet invalid')


def audit_rows(raw, cases):
    lines = raw.splitlines()
    bad = ['[suffix-causal-error]', '[suffix] ', 'MTP init failed', 'retained after failed drain',
           'MTP trunk alloc failed', 'plain decode for this request', 'MTP load failed', 'MTP allocation failed']
    if any(marker in raw for marker in bad):
        raise RuntimeError('fallback/diagnostic error')
    admitted, ended = {}, {}
    for number, line in enumerate(lines):
        if 'event=admitted ' in line or 'event=end ' in line:
            row = fields(line)
            target = admitted if row['event'] == 'admitted' else ended
            key = row.get('id')
            if not key or key in target:
                raise RuntimeError('duplicate lifecycle identity')
            target[key] = dict(row, line=number)
    rows = []
    for line in lines:
        if line.startswith('[suffix-causal] '):
            if len(line.encode()) > 2048:
                raise RuntimeError('diagnostic line budget')
            row = fields(line)
            rows.append({k: v if k in ('request', 'mode') else int(v) for k, v in row.items()})
    if len(rows) != len(cases) or len(admitted) != len(cases) or admitted.keys() != ended.keys():
        raise RuntimeError('incomplete request lifecycle')
    if {r['request'] for r in rows} != admitted.keys():
        raise RuntimeError('diagnostic request identity mismatch')
    for ordinal, (row, case) in enumerate(zip(rows, cases)):
        end = ended[row['request']]
        if end.get('released') != '1' or end['line'] <= admitted[row['request']]['line']:
            raise RuntimeError('slot not released')
        if (row['ordinal'], row['mode'], row['input']) != (ordinal, case['mode'], case['input_tokens']):
            raise RuntimeError('mode/order/token mismatch')
        clocks = [row[k] for k in ['arrive_ns', 'target_done_ns', 'prefill_done_ns', 'selected_ns',
                                   'first_round_ns', 'first_content_ns', 'generation_done_ns']]
        if clocks[0] <= 0 or clocks != sorted(clocks):
            raise RuntimeError('phase ordering incomplete')
        if any(row[k] != v for k, v in case['expected_work'].items()) or row['ready'] or row['hits']:
            raise RuntimeError('zero-hit/work reference changed')
        costs = [row[k] for k in ['init_ns', 'query_ns', 'append_ns', 'release_ns', 'peak_bytes', 'queries']]
        if min(costs) < 0:
            raise RuntimeError('negative cost')
        if case['mode'] in ('N1', 'N2'):
            if any(row[k] for k in ['init_ns', 'query_ns', 'append_ns', 'peak_bytes', 'queries']):
                raise RuntimeError('neural-only arm initialized index')
        elif row['queries'] != row['verify'] or not 0 < row['peak_bytes'] <= 2 * 1024 * 1024:
            raise RuntimeError('query arm missing/beyond resource contract')
    return rows


def audit_server(output, expected_requests):
    raw = (output / 'server.log').read_text()
    step = json.loads((output / 'step.json').read_text())
    if len(step['cases']) != expected_requests:
        raise RuntimeError('audit budget mismatch')
    rows = audit_rows(raw, step['cases'])
    final = [json.loads(line.split('] ', 1)[1]) for line in raw.splitlines()
             if line.startswith('[startup-reservation] ') and '"event":"finalized"' in line]
    if len(final) != 1 or final[0]['status'] != 'PASS' or final[0].get('release_unknown', 0):
        raise RuntimeError('startup reservation finalization failed')
    receipt = [fields(l) for l in raw.splitlines() if l.startswith('[suffix-causal-plan] ')]
    if receipt != [dict(consumed=str(expected_requests), expected=str(expected_requests))]:
        raise RuntimeError('plan consumption mismatch')
    table = [dict(fields(l), line=n) for n,l in enumerate(raw.splitlines()) if l.startswith('[suffix-classes] ')]
    if step['suffix']:
        if (len(table) != 2 or [r['event'] for r in table] != ['init', 'release'] or
                int(table[0]['bytes']) != 248320 or int(table[0]['cap_bytes']) != 262144 or
                int(table[0]['unknown']) != 243 or int(table[1]['bytes']) != 248320):
            raise RuntimeError('S1 table contract changed')
        admit = [n for n,l in enumerate(raw.splitlines()) if 'event=admitted ' in l]
        ends = [n for n,l in enumerate(raw.splitlines()) if 'event=end ' in l]
        if table[0]['line'] >= min(admit) or table[1]['line'] <= max(ends):
            raise RuntimeError('table lifetime mismatch')
    elif table:
        raise RuntimeError('S0 built classification table')
    result = json.loads((output / 'exit.json').read_text())
    if result['server'] != 0 or not result['http_output_checks_passed']:
        raise RuntimeError('unclean HTTP/server exit')
    save(output / 'causal-audit.json', dict(passed=True, rows=rows, table=table, finalization=final))


def cpu_after_first(plan, output):
    target = Path(plan['output']) / 'cpu-contracts.json'
    if target.exists():
        raise RuntimeError('CPU gate cannot rerun')
    started = time.monotonic()
    results = []
    commands = [[plan['contract_binary']], [sys.executable, '-B', str(ROOT / 'tools/mtp_suffix_causal/test_controls.py')]]
    try:
        for i, command in enumerate(commands):
            with (output / f'cpu-{i}.log').open('x') as log:
                value = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                       timeout=max(.01, 120 - (time.monotonic() - started)))
            results.append(dict(command=command, returncode=value.returncode))
            if value.returncode:
                raise RuntimeError('direct CPU contract failed')
    finally:
        save(target, dict(commands=results, elapsed_seconds=time.monotonic()-started,
                          passed=len(results)==2 and all(r['returncode']==0 for r in results)))


def custom_step(plan, name):
    legacy.require_guard()
    step = next(s for s in plan['steps'] if s['name'] == name)
    output = Path(plan['output']) / name
    output.mkdir(exist_ok=False)
    env = {k:v for k,v in legacy.clean_env().items() if not k.startswith('CUPTI_')}
    env['Q4T_SUFFIX_CAUSAL_PLAN'] = step['mode_plan']
    command = [plan['candidate'], 'serve', '--model-dir', plan['model'], '--host', '127.0.0.1',
               '--port', str(plan['port']), '--max-seq', '1', '--max-prefill', '8192',
               '--max-len', '208896', '--max-tokens', '256', '--mtp', '--mtp-k', '3',
               '--mtp-subset-head', plan['artifact']]
    if step['suffix']:
        command += ['--mtp-suffix']
    save(output/'server-command.json', dict(argv=command, environment={'Q4T_SUFFIX_CAUSAL_PLAN':step['mode_plan']}))
    save(output/'step.json', step)
    passed, failure, server = False, None, None
    deadline = time.monotonic() + step['timeout'] - 45
    def check():
        if time.monotonic() >= deadline:
            raise TimeoutError('step deadline reserved cleanup')
        if server is not None and server.poll() is not None:
            raise RuntimeError('server exited unexpectedly')
    with (output/'server.log').open('x') as log:
        try:
            http.event(output,'server_before_start')
            server = subprocess.Popen(command,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT)
            (output/'server.pid').write_text(str(server.pid)+'\n')
            http.event(output,'server_started',pid=server.pid)
            startup = min(deadline,time.monotonic()+900)
            while time.monotonic() < startup:
                check()
                if 'serving on port' in (output/'server.log').read_text(): break
                time.sleep(1)
            else: raise TimeoutError('startup limit')
            capacity = http.capacity_evidence((output/'server.log').read_text(),208896)
            save(output/'capacity.json',capacity)
            http.event(output,'server_ready',pid=server.pid,capacity=capacity)
            if not capacity['matches_requested']: raise RuntimeError('capacity mismatch')
            results=[]
            for case in step['cases']:
                check()
                batch=admission.run_cases(plan,dict(step,cases=[case]),output,server,env,capacity,client_check=check)
                results.extend(batch)
                save(output/'results.json',results)
                result=batch[0]
                if result['outputs'] != [case['expected_output_sha256']] or any(
                    r['actual_output'] != 256 or r['finish'] != ['length'] for r in result['responses']):
                    raise RuntimeError('output/finish reference differs')
                # The RAII log follows slot release and request
                # unregistration; allow bounded log visibility, never reissue HTTP.
                until=time.monotonic()+5
                while (output/'server.log').read_text().count('[suffix-causal] ') < len(results):
                    check()
                    if time.monotonic() >= until: raise RuntimeError('missing request summary')
                    time.sleep(.01)
                audit_rows((output/'server.log').read_text(),step['cases'][:len(results)])
                if name==plan['steps'][0]['name'] and len(results)==1:
                    save(Path(plan['output'])/'first-http-gate.json',dict(passed=True,output=result['outputs'][0],
                         unix=time.time(),scope='output, finish, work, slot release; no broad quality claim'))
                    cpu_after_first(plan,output)
            passed=True
        except BaseException as error:
            failure=repr(error)
            raise
        finally:
            if server is not None:
                http.event(output,'server_before_shutdown',pid=server.pid)
                if server.poll() is None:
                    server.terminate()
                    try: server.wait(timeout=30)
                    except subprocess.TimeoutExpired: server.kill(); server.wait(timeout=10)
                http.event(output,'server_after_shutdown',pid=server.pid,server_returncode=server.returncode)
            save(output/'exit.json',dict(server=server.returncode if server else None,
                 http_output_checks_passed=passed and server is not None and server.returncode==0,failure=failure))
    if not passed or server.returncode != 0: raise RuntimeError('HTTP group incomplete')


if __name__ == '__main__':
    def interrupted(signum, frame):
        raise KeyboardInterrupt('owned stage terminated')
    signal.signal(signal.SIGTERM, interrupted)
    rerun.validate=validate
    rerun.custom_step=custom_step
    rerun.admission.audit_server=audit_server
    rerun.main()

"""Two-start, plain-decode HTTP gate for ordered sparse softmax parallelism."""
import argparse
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
import time

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    'qsa_http_base', ROOT / 'tools/mtp_selected_head/stage.py')
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
legacy, http, telemetry = base.legacy, base.http, base.telemetry
sha, save = base.sha, base.save
ORDER = [('plain', 'parent'), ('plain', 'candidate')]
CASES = [('context-1024', 3, 256, 1), ('context-45056', 3, 256, 1),
         ('quality', 11, 32, 1)]


def require(value, message):
    if not value:
        raise ValueError(message)


def server_command(plan, arm):
    return [plan[arm], 'serve', '--model-dir', plan['model'],
            '--host', '127.0.0.1', '--port', str(plan['port']), '--max-seq', '1',
            '--max-prefill', '8192', '--max-len', '208896', '--max-tokens', '256',
            '--startup-plan', 'off', '--no-mtp']


def validate(plan, *, executing=True):
    require(plan['stage'] == 'QSA_SOFTMAX_FEASIBILITY' and
            [(s['head'], s['arm']) for s in plan['steps']] == ORDER and
            (plan['http_requests'], plan['http_loads']) == (34, 2) and
            plan['stage_timeout_seconds'] == 1140 and
            plan['minimum_ttft_gain'] == .03, 'frozen two-arm scope')
    if executing and time.time() >= plan['task_deadline_unix']:
        raise TimeoutError('absolute deadline')
    for step in plan['steps']:
        require((step['http_requests'], step['http_loads'], step['timeout']) ==
                (17, 1, 570) and not step.get('env'), 'per-arm budget/environment')
        require([(c['id'], c['count'], c['tokens'], c['parallel'])
                 for c in step['cases']] == CASES, 'case budget')
        require(step['server_command'] == server_command(plan, step['arm']),
                'server configuration')


def plain_log(raw):
    require(raw.count('[q4t] MTP disabled; plain decode') == 1 and
            '[q4t][startup-plan] mode=off active=0' in raw,
            'plain decode/allocation mode mismatch')
    for marker in ('MTP loaded', '[mtp-accept]', '[mtp-timing]',
                   '[layer-timing]', '[sub-timing]', '[qsa-subtiming]',
                   '[moe-subtiming]', '[hc-mix-subtiming]'):
        require(marker not in raw, 'unexpected MTP/profiling: ' + marker)


def custom_step(plan, name):
    legacy.require_guard()
    step = next(s for s in plan['steps'] if s['name'] == name)
    out = Path(plan['output']) / name
    out.mkdir()
    save(out / 'step.json', step)
    base.multi.ROOT = Path(plan['execution_tools_root'])
    env = {k: v for k, v in legacy.clean_env().items()
           if not k.startswith('CUPTI_')}
    command = step['server_command']
    save(out / 'server-command.json', dict(argv=command, environment={
        k: v for k, v in env.items() if k.startswith('Q4T_')}))
    deadline = min(time.monotonic() + step['timeout'] - 40,
                   time.monotonic() + plan['task_deadline_unix'] - time.time() - 40)
    server, failure, passed = None, None, False

    def check():
        if time.monotonic() >= deadline:
            raise TimeoutError('bounded HTTP deadline')
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
                    raise TimeoutError('model startup deadline')
                time.sleep(.5)
            raw = (out / 'server.log').read_text()
            capacity = http.capacity_evidence(raw, 208896)
            save(out / 'capacity.json', capacity)
            require(capacity['matches_requested'], 'capacity reduced')
            plain_log(raw)
            http.event(out, 'server_ready', pid=server.pid, capacity=capacity)
            results = []
            for case in step['cases']:
                result = base.multi.case(plan, step, case, out, env, check)
                results.append(result)
                save(out / 'results.json', results)
                if step['arm'] == 'candidate':
                    old = json.loads((Path(plan['output']) / plan['steps'][0]['name'] /
                                      case['id'] / 'result.json').read_text())
                    same = base.identity(old) == base.identity(result)
                    save(out / (case['id'] + '-comparison.json'),
                         dict(parent_candidate_outputs_identical=same))
                    require(same, 'sparse softmax final output mismatch')
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


def audit_server(out, expected):
    audit = base.audit_server(out, expected)
    raw = (out / 'server.log').read_text()
    plain_log(raw)
    active, completed = None, []
    for line in raw.splitlines():
        if not line.startswith('[q4t][request] '):
            continue
        fields = dict(re.findall(r'(\w+)=(\S+)', line))
        if fields.get('event') == 'admitted':
            require(active is None and fields['id'] not in completed,
                    'overlapping/duplicate request')
            active = fields['id']
        elif fields.get('event') == 'end':
            require(active == fields['id'] and fields['outcome'] == 'success',
                    'unpaired/failed request')
            completed.append(active)
            active = None
    require(active is None and len(completed) == expected, 'request coverage')
    return dict(audit, request_ids=completed, plain=True, profiling=False)


def raw_case(plan, case, directory):
    """Reconstruct the sealed local EvalScope database independently of JSON."""
    databases = list(directory.rglob('benchmark_data.db'))
    require(len(databases) == 1, 'database count')
    with sqlite3.connect(databases[0].as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        raw_rows = db.execute('SELECT * FROM result ORDER BY start_time').fetchall()
    derived = json.loads((directory / 'result.json').read_text())
    require(len(raw_rows) == len(derived['rows']) == case['count'], 'raw row count')
    rows = []
    for row, recorded in zip(raw_rows, derived['rows']):
        # Only this run's hash-sealed, trusted local client output is decoded.
        messages = pickle.loads(base64.b64decode(row['response_messages']))
        choices = [c for m in messages for c in m.get('choices', [])]
        content = ''.join(c.get('delta', c.get('message', {})).get('content', '')
                          for c in choices)
        ids = {m['id'] for m in messages if m.get('id')}
        request = json.loads(row['request'])
        require(len(ids) == 1 and request['max_tokens'] == case['tokens'] and
                request['stream'] and row['success'], 'raw HTTP identity/contract')
        value = dict(response_id=next(iter(ids)), input_tokens=row['prompt_tokens'],
            output_tokens=row['completion_tokens'], text=content,
            prompt_sha256=hashlib.sha256(request['prompt'].encode()).hexdigest(),
            output_sha256=hashlib.sha256(content.encode()).hexdigest(),
            finish=[c['finish_reason'] for c in choices if c.get('finish_reason')],
            ttft=row['first_chunk_latency'], latency=row['latency'],
            start=row['start_time'], end=row['completed_time'])
        require(all(recorded[k] == v for k, v in value.items()), 'JSON/raw mismatch')
        require(all(math.isfinite(value[k]) for k in ('ttft', 'latency')) and
                0 < value['ttft'] < value['latency'] and
                case['min'] <= value['input_tokens'] <= case['max'],
                'raw timing/input contract')
        if case['id'] == 'quality':
            expected = plan['quality'][value['prompt_sha256']]
            require(content.strip() == expected['expected'] and
                    value['input_tokens'] == expected['length'] and
                    value['finish'] == ['stop'], 'raw quality contract')
        else:
            require(value['output_tokens'] == case['tokens'] and
                    value['finish'] == ['length'], 'raw output/finish contract')
        rows.append(value)
    if case['id'] == 'quality':
        require({r['prompt_sha256'] for r in rows} == set(plan['quality']),
                'raw quality coverage')
    return dict(id=case['id'], rows=rows, database=str(databases[0]))


def metrics(rows):
    require(len(rows) == 3, 'metric sample count')
    intervals = [r['latency'] - r['ttft'] for r in rows]
    return dict(mean_ttft=sum(r['ttft'] for r in rows) / len(rows),
        aggregate_decode=sum(r['output_tokens'] - 1 for r in rows) / sum(intervals),
        aggregate_e2e=sum(r['latency'] for r in rows),
        ttft=[r['ttft'] for r in rows], latency=[r['latency'] for r in rows],
        decode=[(r['output_tokens'] - 1) / t for r, t in zip(rows, intervals)])


def analyze(plan, lease):
    validate(plan, executing=False)
    base.monitored_stage.require_complete_monitor(plan)
    legacy.verify_files(plan)
    guard = json.loads((lease / 'exit.json').read_text())
    require(guard['status'] == 'COMPLETED', 'outer GPU lease did not complete')
    output = Path(plan['output'])
    seal = json.loads((output / 'files.json').read_text())
    for name, digest in seal.items():
        require(sha(output / name) == digest, 'raw seal changed: ' + name)
    arms = {}
    for step in plan['steps']:
        folder = output / step['name']
        audit = audit_server(folder, 17)
        cases = [raw_case(plan, c, folder / c['id']) for c in step['cases']]
        responses = [r['response_id'] for c in cases for r in c['rows']]
        require(len(responses) == len(set(responses)) == 17 and
                set(responses) == set(audit['request_ids']), 'HTTP/server coverage')
        arms[step['arm']] = dict(cases=cases, audit=audit, quality_passed=11,
            metrics={c['id']: metrics(c['rows']) for c in cases[:2]})
    parent, candidate = arms['parent'], arms['candidate']
    same = all(base.identity(a) == base.identity(b) for a, b in
               zip(parent['cases'], candidate['cases']))
    require(same, 'output contract changed')
    contexts = {}
    for name, *_ in CASES[:2]:
        p, c = [arm['metrics'][name] for arm in (parent, candidate)]
        contexts[name] = dict(parent=p, candidate=c,
            ttft_gain=1 - c['mean_ttft'] / p['mean_ttft'],
            decode_gain=c['aggregate_decode'] / p['aggregate_decode'] - 1,
            e2e_change=c['aggregate_e2e'] / p['aggregate_e2e'] - 1)
    target = contexts['context-45056']
    passed = target['ttft_gain'] >= plan['minimum_ttft_gain'] and target['e2e_change'] < 0
    result = dict(contexts=contexts, outputs_identical=same, arms=arms,
        performance_accepted=False, independent_numerical_contract_tested=False,
        statistics='DESCRIPTIVE_SINGLE_START_PER_ARM_NOT_NONINFERIORITY',
        cold_request='Each arm starts at 1K; every request retained individually.',
        comparison='Short-accepted R1/R2 parent; not the installed default.',
        decision='GO_FOR_FULL_EVALUATION' if passed else 'NO_GO_FEASIBILITY_TARGET')
    save(output.parent / 'decision.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--step')
    parser.add_argument('--analyze-only', action='store_true')
    parser.add_argument('--lease', type=Path)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    if args.analyze_only:
        if args.lease is None:
            parser.error('--analyze-only requires completed --lease')
        result = analyze(plan, args.lease)
        print(json.dumps({k: result[k] for k in ('decision', 'contexts')}, indent=2))
        return
    validate(plan)
    if args.step:
        custom_step(plan, args.step)
    else:
        base.monitored_stage.run_stage(legacy, telemetry, plan, args.plan,
                                      audit_server=audit_server)


if __name__ == '__main__':
    def interrupted(*_):
        raise KeyboardInterrupt('sparse softmax stage interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    main()

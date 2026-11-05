"""Frozen four-arm HTTP gate for consumed BF16 extend-head rows."""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import signal
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    'selected_head_multi', ROOT / 'tools/mtp_suffix_multi/run_stage.py')
multi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(multi)
legacy, http, telemetry = multi.legacy, multi.http, multi.reuse.telemetry
import monitored_stage

sha, save = legacy.sha, legacy.save


def validate(plan):
    if (plan['stage'] != 'SELECTED_HEAD_FEASIBILITY' or
            [(s['head'], s['arm']) for s in plan['steps']] != [
                ('full', 'parent'), ('full', 'candidate'),
                ('subset', 'candidate'), ('subset', 'parent')] or
            (plan['http_requests'], plan['http_loads']) != (56, 4) or
            not 0 < plan['stage_timeout_seconds'] <= 1800 or
            plan['minimum_decode_gain'] != .03):
        raise ValueError('frozen four-arm scope')
    if time.time() >= plan['task_deadline_unix']:
        raise TimeoutError('absolute deadline')
    for step in plan['steps']:
        if (step['http_requests'], step['http_loads']) != (14, 1):
            raise ValueError('per-arm budget')
        if [(c['id'], c['count'], c['tokens'], c['parallel'])
                for c in step['cases']] != [
                    ('context-1024', 3, 256, 1), ('quality', 11, 32, 1)]:
            raise ValueError('case budget')


def identity(case):
    return sorted((r['prompt_sha256'], r['input_tokens'], r['output_sha256'],
                   r['output_tokens'], tuple(r['finish'])) for r in case['rows'])


def custom_step(plan, name):
    legacy.require_guard()
    step = next(s for s in plan['steps'] if s['name'] == name)
    out = Path(plan['output']) / name
    out.mkdir()
    save(out / 'step.json', step)
    multi.ROOT = Path(plan['execution_tools_root'])
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
            if (not capacity['matches_requested'] or
                    '[q4t][startup-plan] mode=off active=0' not in raw or
                    'MTP loaded (k=3 max_seq=1)' not in raw):
                raise RuntimeError('capacity/MTP/allocation mode mismatch')
            http.event(out, 'server_ready', pid=server.pid, capacity=capacity)
            results = []
            for case in step['cases']:
                result = multi.case(plan, step, case, out, env, check)
                results.append(result)
                save(out / 'results.json', results)
                prior = [s for s in plan['steps'] if s['head'] == step['head'] and
                         s['name'] < step['name']]
                if prior:
                    old = json.loads((Path(plan['output']) / prior[0]['name'] /
                                      case['id'] / 'result.json').read_text())
                    same = identity(old) == identity(result)
                    save(out / (case['id'] + '-comparison.json'),
                         dict(same_head_parent_candidate_identical=same))
                    if not same:
                        raise RuntimeError('selected head final output mismatch')
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
    raw = (out / 'server.log').read_text()
    ended = json.loads((out / 'exit.json').read_text())
    if ended['server'] != 0 or not ended['http_output_checks_passed']:
        raise RuntimeError('HTTP/server exit failure')
    for marker in ('MTP load failed', 'plain decode for this request',
                   'retained after failed drain', 'MTP allocation failed'):
        if marker in raw:
            raise RuntimeError('unexpected fallback/drain: ' + marker)
    admitted, released = {}, {}
    for number, line in enumerate(raw.splitlines()):
        if 'event=admitted ' not in line and 'event=end ' not in line:
            continue
        row = dict(re.findall(r'(\w+)=(\S+)', line))
        target = admitted if row['event'] == 'admitted' else released
        if not row.get('id') or row['id'] in target:
            raise RuntimeError('duplicate/missing lifecycle')
        target[row['id']] = dict(row, line=number)
    if (len(admitted) != expected or admitted.keys() != released.keys() or
            any(r.get('released') != '1' or r['line'] <= admitted[k]['line']
                for k, r in released.items())):
        raise RuntimeError('incomplete request release')
    quota = [json.loads(line.split('] ', 1)[1]) for line in raw.splitlines()
             if line.startswith('[q4t][cuda-quota] ') and
             '"event":"server_destroyed"' in line]
    if (len(quota) != 1 or quota[0]['poisoned'] or any(quota[0][k] != 0 for k in
            ('pending_bytes', 'unknown_bytes', 'contract_failures', 'backend_failures')) or
            quota[0]['charged_bytes'] != quota[0]['live_bytes']):
        raise RuntimeError('CUDA accounting failure')
    # This is before process exit; process-lifetime allocations may still live.
    # Preserve that accounting separately from the outer GPU-context release.
    return dict(admitted=len(admitted), released=len(released), quota=quota[0])


def analyze(plan):
    monitored_stage.require_complete_monitor(plan)
    legacy.verify_files(plan)
    arms = {}
    for step in plan['steps']:
        out = Path(plan['output']) / step['name']
        audit = audit_server(out, 14)
        cases = json.loads((out / 'results.json').read_text())
        rows = cases[0]['rows']
        times = [r['latency'] - r['ttft'] for r in rows]
        if any(t <= 0 for t in times):
            raise RuntimeError('nonpositive decode interval')
        arms[(step['head'], step['arm'])] = dict(cases=cases, audit=audit,
            decode=[(r['output_tokens'] - 1) / t for r, t in zip(rows, times)],
            aggregate_decode=sum(r['output_tokens'] - 1 for r in rows) / sum(times),
            latency=[r['latency'] for r in rows], ttft=[r['ttft'] for r in rows],
            aggregate_e2e=sum(r['latency'] for r in rows),
            first_request_separate=True, quality_passed=11)
    comparisons = {}
    for head in ('full', 'subset'):
        parent, candidate = [arms[(head, arm)] for arm in ('parent', 'candidate')]
        same = all(identity(a) == identity(b) for a, b in
                   zip(parent['cases'], candidate['cases']))
        gain = candidate['aggregate_decode'] / parent['aggregate_decode'] - 1
        e2e = candidate['aggregate_e2e'] / parent['aggregate_e2e'] - 1
        comparisons[head] = dict(parent=parent, candidate=candidate,
            outputs_identical=same, decode_gain=gain, e2e_change=e2e,
            target_met=same and gain >= plan['minimum_decode_gain'] and e2e < 0)
    result = dict(comparisons=comparisons, performance_accepted=False,
        statistics='DESCRIPTIVE_SINGLE_START_NOT_NONINFERIORITY',
        decision='GO_FOR_FULL_EVALUATION' if all(c['target_met'] for c in
            comparisons.values()) else 'NO_GO_FEASIBILITY_TARGET')
    save(Path(plan['output']).parent / 'decision.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--step')
    parser.add_argument('--analyze-only', action='store_true')
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    if args.analyze_only:
        print(json.dumps(analyze(plan), indent=2))
        return
    validate(plan)
    if args.step:
        custom_step(plan, args.step)
        return
    monitored_stage.run_stage(legacy, telemetry, plan, args.plan,
                              audit_server=audit_server)
    result = analyze(plan)
    print(json.dumps(dict(decision=result['decision'],
        observations={h: {k: c[k] for k in ('decode_gain', 'e2e_change', 'target_met')}
                      for h, c in result['comparisons'].items()}), indent=2))


if __name__ == '__main__':
    def interrupted(*_):
        raise KeyboardInterrupt('selected head stage interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    main()

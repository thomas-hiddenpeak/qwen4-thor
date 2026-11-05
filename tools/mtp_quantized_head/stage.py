"""Four-arm HTTP feasibility gate; no retries, deployment or kernel benchmarks."""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import signal
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    'quantized_multi', ROOT / 'tools/mtp_suffix_multi/run_stage.py')
multi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(multi)
legacy, http, telemetry = multi.legacy, multi.http, multi.reuse.telemetry
save, sha = legacy.save, legacy.sha


def validate(plan):
    if (plan['stage'] != 'QUANTIZED_HEAD_FEASIBILITY' or
            [s['head'] for s in plan['steps']] !=
            ['plain', 'full', 'nvfp4', 'subset'] or
            (plan['http_requests'], plan['http_loads'],
             plan['stage_timeout_seconds']) != (56, 4, 1800)):
        raise ValueError('frozen feasibility scope')
    if time.time() >= plan['task_deadline_unix']:
        raise TimeoutError('frozen deadline')
    for step in plan['steps']:
        if (step['http_loads'], step['http_requests']) != (1, 14):
            raise ValueError('per-arm request budget')
        if [(c['id'], c['count'], c['tokens'], c['parallel'])
                for c in step['cases']] != [
                    ('context-1024', 3, 256, 1), ('quality', 11, 32, 1)]:
            raise ValueError('case scope')


def identity(case):
    return sorted((r['prompt_sha256'], r['input_tokens'], r['output_sha256'],
                   r['output_tokens'], tuple(r['finish'])) for r in case['rows'])


def custom_step(plan, name):
    legacy.require_guard()
    step = next(s for s in plan['steps'] if s['name'] == name)
    out = Path(plan['output']) / name
    out.mkdir()
    save(out / 'step.json', step)
    # Collector code belongs to this worktree; only installed evalscope is
    # reused from the original checkout. No client connection/warmup probes.
    multi.ROOT = Path(plan['execution_tools_root'])
    command = step['server_command']
    env = {k: v for k, v in legacy.clean_env().items()
           if not k.startswith('CUPTI_')}
    save(out / 'server-command.json', dict(argv=command,
        environment={k: v for k, v in env.items() if k.startswith('Q4T_')}))
    deadline = min(time.monotonic() + step['timeout'] - 40,
        time.monotonic() + plan['task_deadline_unix'] - time.time() - 40)
    server, passed, failure = None, False, None

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
            if not capacity['matches_requested']:
                raise RuntimeError('capacity changed')
            if '[q4t][startup-plan] mode=off active=0' not in raw:
                raise RuntimeError('different allocation mode')
            quantized = 'NVFP4 draft head W4A16 rows=248320 hidden=2560' in raw
            if quantized != (step['head'] == 'nvfp4'):
                raise RuntimeError('head selection mismatch')
            if step['head'] != 'plain' and 'MTP loaded (k=3 max_seq=1)' not in raw:
                raise RuntimeError('MTP mode mismatch')
            http.event(out, 'server_ready', pid=server.pid, capacity=capacity)
            results = []
            for case in step['cases']:
                result = multi.case(plan, step, case, out, env, check)
                results.append(result)
                save(out / 'results.json', results)
                if step['head'] == 'nvfp4':
                    reference = Path(plan['output']) / '01-full' / case['id']
                    old = json.loads((reference / 'result.json').read_text())
                    same = identity(old) == identity(result)
                    save(out / (case['id'] + '-comparison.json'),
                         dict(full_output_identical=same))
                    if not same:
                        raise RuntimeError('quantized final output differs from full')
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
    result = json.loads((out / 'exit.json').read_text())
    if result['server'] != 0 or not result['http_output_checks_passed']:
        raise RuntimeError('HTTP/server exit failure')
    for marker in ('MTP load failed', 'plain decode for this request',
                   'retained after failed drain', 'MTP allocation failed'):
        if marker in raw:
            raise RuntimeError('unexpected fallback/drain: ' + marker)
    admitted, ended = {}, {}
    for number, line in enumerate(raw.splitlines()):
        if 'event=admitted ' not in line and 'event=end ' not in line:
            continue
        row = dict(re.findall(r'(\w+)=(\S+)', line))
        target = admitted if row['event'] == 'admitted' else ended
        if not row.get('id') or row['id'] in target:
            raise RuntimeError('duplicate/missing request lifecycle')
        target[row['id']] = dict(row, line=number)
    if (len(admitted) != expected or admitted.keys() != ended.keys() or
            any(r.get('released') != '1' or r['line'] <= admitted[k]['line']
                for k, r in ended.items())):
        raise RuntimeError('incomplete request release')
    quota = [json.loads(line.split('] ', 1)[1]) for line in raw.splitlines()
             if line.startswith('[q4t][cuda-quota] ') and
             '"event":"server_destroyed"' in line]
    if (len(quota) != 1 or quota[0]['poisoned'] or any(quota[0][key] != 0 for key
            in ('live_bytes', 'charged_bytes', 'pending_bytes', 'unknown_bytes',
                'entries', 'contract_failures', 'backend_failures'))):
        raise RuntimeError('CUDA ownership did not drain')
    save(out / 'server-audit.json', dict(admitted=admitted, ended=ended,
         quota=quota[0], allocation_mode='off'))


def analyze(plan):
    result = dict(arms={}, performance_accepted=False,
                  statistics='DESCRIPTIVE_SINGLE_START_NOT_NONINFERIORITY')
    full = None
    for step in plan['steps']:
        out = Path(plan['output']) / step['name']
        audit_server(out, 14)
        cases = json.loads((out / 'results.json').read_text())
        rows = cases[0]['rows']
        speeds = [(r['output_tokens'] - 1) / (r['latency'] - r['ttft'])
                  for r in rows]
        metrics = dict(decode=speeds, decode_median=statistics.median(speeds),
            ttft=[r['ttft'] for r in rows],
            ttft_median=statistics.median(r['ttft'] for r in rows),
            latency=[r['latency'] for r in rows], quality_passed=11,
            first_request_separate=True, full_output_identical=None)
        if step['head'] == 'full':
            full = cases
        if full is not None:
            metrics['full_output_identical'] = all(
                identity(a) == identity(b) for a, b in zip(full, cases))
        result['arms'][step['head']] = metrics
    baseline, candidate = [result['arms'][k] for k in ('full', 'nvfp4')]
    result['decode_gain'] = candidate['decode_median'] / baseline['decode_median'] - 1
    result['ttft_change'] = candidate['ttft_median'] / baseline['ttft_median'] - 1
    result['decision'] = ('GO_FOR_FULL_EVALUATION' if
        result['decode_gain'] >= .05 and candidate['full_output_identical'] else
        'NO_GO_FEASIBILITY_TARGET')
    save(Path(plan['output']) / 'decision.json', result)
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
    telemetry.configure_sources(plan['telemetry_sources'])
    legacy.sample_memory = telemetry.sample
    legacy.audit_server = audit_server
    legacy.run_stage(plan, args.plan)
    analyze(plan)


if __name__ == '__main__':
    def interrupted(*_):
        raise KeyboardInterrupt('quantized head stage interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    main()

"""Frozen zero-hit diagnostic HTTP stage; no runtime or admission changes."""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import telemetry

SPEC = importlib.util.spec_from_file_location(
    'suffix_admission_reused', ROOT / 'tools/mtp_suffix_admission/run_stage.py')
admission = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(admission)
legacy = admission.legacy
http = admission.http_runner
save = legacy.save


def validate(plan):
    expected = [
        ('first-http', 'full', True, False),
        ('subset-off-a1', 'subset', False, True),
        ('subset-on-b', 'subset', True, True),
        ('subset-off-a2', 'subset', False, True),
        ('full-on', 'full', True, True),
        ('full-off', 'full', False, True)]
    actual = [(s['name'], s['head'], s['suffix'], s['timing'])
              for s in plan['steps']]
    if actual != expected:
        raise ValueError('diagnostic order differs from frozen six services')
    if plan['http_requests'] != 26 or plan['http_loads'] != 6:
        raise ValueError('diagnostic budget changed')
    if plan['stage_timeout_seconds'] != 3600:
        raise ValueError('diagnostic wall budget changed')
    cases = ['context-8192', 'context-45056', 'context-204800',
             'repeat-code', 'repeat-text']
    for i, s in enumerate(plan['steps']):
        wanted = cases[:1] if i == 0 else cases
        if ([c['id'] for c in s['cases']] != wanted or
                any(c['repeats'] != 1 or c['role'] != 'development_diagnostic'
                    for c in s['cases']) or
                s['http_requests'] != len(wanted) or s['http_loads'] != 1):
            raise ValueError('diagnostic case/count identity changed')


def custom_step(plan, name):
    legacy.require_guard()
    step = next(s for s in plan['steps'] if s['name'] == name)
    output = Path(plan['output']) / name
    output.mkdir(parents=True, exist_ok=False)
    env = legacy.clean_env()
    if step['timing']:
        env['Q4T_MTP_TIMING'] = '1'
    command = [plan['candidate'], 'serve', '--model-dir', plan['model'],
               '--port', str(plan['port']), '--max-seq', '1', '--max-prefill',
               '8192', '--max-len', '208896', '--max-tokens', '256',
               '--mtp', '--mtp-k', '3']
    if step['head'] == 'subset':
        command += ['--mtp-subset-head', plan['artifact']]
    if step['suffix']:
        command += ['--mtp-suffix']
    save(output / 'server-command.json', dict(
        argv=command, environment={k:v for k,v in env.items() if k.startswith('Q4T_')},
        measurement_role='diagnostic' if step['timing'] else 'first_http_gate',
        diagnostic_only=step['timing'], performance_acceptance_sample=False,
        performance_acceptance=False))
    save(output / 'step.json', step)
    passed, failure, server = False, None, None
    with (output / 'server.log').open('x') as log:
        try:
            http.event(output, 'server_before_start')
            server = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log,
                                      stderr=subprocess.STDOUT)
            (output / 'server.pid').write_text(str(server.pid) + '\n')
            http.event(output, 'server_started', pid=server.pid)
            deadline = time.monotonic() + 900
            while time.monotonic() < deadline:
                if server.poll() is not None:
                    raise RuntimeError('server exited during startup')
                if 'serving on port' in (output / 'server.log').read_text():
                    break
                time.sleep(1)
            else:
                raise TimeoutError('diagnostic startup exceeded 900 seconds')
            capacity = http.capacity_evidence((output / 'server.log').read_text(), 208896)
            save(output / 'capacity.json', capacity)
            http.event(output, 'server_ready', pid=server.pid, capacity=capacity)
            if not capacity['matches_requested']:
                raise RuntimeError('effective capacity mismatch')
            # The existing collector uses evalscope and writes raw HTTP/SSE,
            # SQLite, first-request markers and boundary timestamps.
            results = []
            for case in step['cases']:
                batch = admission.run_cases(
                    plan, dict(step, cases=[case]), output, server, env, capacity)
                results.extend(batch)
                save(output / 'results.json', results)
                result = batch[0]
                if result['outputs'] != [case['expected_output_sha256']]:
                    raise RuntimeError('diagnostic output differs from frozen reference: ' + case['id'])
                row = result['responses'][0]
                if row['actual_output'] != 256 or row['finish'] != ['length']:
                    raise RuntimeError('diagnostic output length/finish changed')
            save(output / 'output-reference-check.json', dict(
                passed=True, reference='previous same-binary same-head full output hashes',
                arbitrary_input_equivalence=False))
            passed = True
        except BaseException as error:
            failure = repr(error)
            raise
        finally:
            if server is not None:
                http.event(output, 'server_before_shutdown', pid=server.pid)
                if server.poll() is None:
                    server.terminate()
                    try:
                        server.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait(timeout=10)
                http.event(output, 'server_after_shutdown', pid=server.pid,
                           server_returncode=server.returncode)
            clean = passed and server is not None and server.returncode == 0
            save(output / 'exit.json', dict(server=server.returncode if server else None,
                http_output_checks_passed=clean, failure=failure,
                diagnostic_only=step['timing'], performance_acceptance_sample=False,
                performance_acceptance=False))
    if not clean:
        raise RuntimeError('diagnostic group failed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', required=True, type=Path)
    parser.add_argument('--custom-step')
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    validate(plan)
    if args.custom_step:
        custom_step(plan, args.custom_step)
    else:
        legacy.require_guard()
        telemetry.configure_sources(plan['telemetry_sources'])
        legacy.sample_memory = telemetry.sample
        legacy.audit_server = admission.audit_server
        try:
            legacy.run_stage(plan, args.plan.resolve())
        finally:
            # The sampler can fail between the legacy driver's last heartbeat
            # check and its join. Preserve its raw state/seal, and separately
            # reject any such final failure before the guard sees a clean exit.
            state_path = Path(plan['output']) / 'state.json'
            if state_path.is_file():
                state = json.loads(state_path.read_text())
                failed = bool(state.get('monitor_failures') or
                              state.get('cleanup_errors') or
                              state.get('status') == 'ENV_ABORT')
                save(args.plan.resolve().parent / 'postflight.json', dict(
                    status='ENV_ABORT' if failed else state.get('status'),
                    state_sha256=legacy.sha(state_path),
                    monitor_failures=state.get('monitor_failures', []),
                    cleanup_errors=state.get('cleanup_errors', []),
                    note='Final sampler join reviewed; original raw state/seal remain unchanged.'))
                if failed:
                    raise legacy.EnvironmentAbort('final telemetry/cleanup failure')


if __name__ == '__main__':
    main()

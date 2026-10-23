"""Frozen repair follow-up: normal HTTP, first-long controls and phase diagnosis."""
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


from rerun_spec import validate


def custom_step(plan, name):
    legacy.require_guard()
    step = next(s for s in plan['steps'] if s['name'] == name)
    output = Path(plan['output']) / name
    output.mkdir(parents=True, exist_ok=False)
    env = legacy.clean_env()
    if step['timing']:
        env['Q4T_MTP_TIMING'] = '1'
        env['Q4T_LAYER_TIMING'] = '1'
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
        measurement_role=step['measurement_role'],
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
                if result['outputs'] != [case['expected_output_sha256']] * case['repeats']:
                    raise RuntimeError('diagnostic output differs from frozen reference: ' + case['id'])
                if (len(result['responses']) != case['repeats'] or
                        any(row['actual_output'] != 256 or row['finish'] != ['length']
                            for row in result['responses'])):
                    raise RuntimeError('diagnostic output length/finish changed')
            save(output / 'output-reference-check.json', dict(
                passed=True, reference='previous same-binary same-head full output hashes',
                arbitrary_input_equivalence=False))
            raw_log = (output / 'server.log').read_text()
            if '[layer-timing] sync failed:' in raw_log:
                raise RuntimeError('layer event timing failed')
            if step['timing'] and '[layer-timing] T=' not in raw_log:
                raise RuntimeError('missing frozen layer timing observation')
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
        monitor = {}
        final_barrier = {}
        original_verify = legacy.verify_files
        verify_calls = 0

        def sample(output, stop, failures, heartbeat):
            monitor.update(failures=failures, heartbeat=heartbeat)
            telemetry.sample(output, stop, failures, heartbeat)

        def verify_with_final_flush(value):
            nonlocal verify_calls
            original_verify(value)
            verify_calls += 1
            if verify_calls != 2:
                return
            # The second verification follows every service shutdown and the
            # driver's child/process cleanup. Keep monitoring until a complete
            # sample has started and flushed after this boundary. Do not end
            # sampling early to avoid the process-exit path being investigated.
            boundary = time.monotonic()
            deadline = boundary + value['resource_sample_max_age_seconds']
            while True:
                if monitor.get('failures'):
                    raise legacy.EnvironmentAbort('final monitor flush failed: ' +
                                                  repr(monitor['failures']))
                heartbeat = dict(monitor.get('heartbeat', {}))
                flushed = heartbeat.get('last_sample', 0)
                sample_started = flushed - heartbeat.get('last_cycle_duration_seconds', 0)
                if ('last_cycle_duration_seconds' in heartbeat and
                        sample_started >= boundary):
                    final_barrier.update(boundary_monotonic=boundary,
                                         sample_started_monotonic=sample_started,
                                         flushed_monotonic=flushed)
                    return
                if time.monotonic() >= deadline:
                    raise legacy.EnvironmentAbort('no final post-shutdown monitor flush')
                time.sleep(.05)

        legacy.sample_memory = sample
        legacy.verify_files = verify_with_final_flush
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
                final_errors = []
                summary_path = Path(plan['output']) / 'telemetry-summary.json'
                summary_sha = None
                try:
                    summary = json.loads(summary_path.read_text())
                    summary_sha = legacy.sha(summary_path)
                    if (summary['sampler_status'] != 'STOPPED' or
                            summary['sampler_failure'] is not None or
                            summary['failures_at_exit'] or
                            summary['unflushed_attempts'] != 0 or
                            summary['sample_attempts'] != summary['flushed_samples'] or
                            summary['flushed_samples'] < 1):
                        final_errors.append('final sampler summary is incomplete/failed')
                    if state.get('status') == 'MEASURED_REQUIRES_REVIEW' and (
                            not final_barrier or summary['last_flushed_heartbeat'] -
                            summary['final_successful_cycle_duration_seconds'] <
                            final_barrier['boundary_monotonic']):
                        final_errors.append('final summary lacks post-shutdown flush')
                except (OSError, KeyError, ValueError, TypeError) as error:
                    final_errors.append('final sampler summary: ' + repr(error))
                failed = bool(state.get('monitor_failures') or
                              state.get('cleanup_errors') or
                              state.get('status') == 'ENV_ABORT' or final_errors)
                save(args.plan.resolve().parent / 'postflight.json', dict(
                    status='ENV_ABORT' if failed else state.get('status'),
                    state_sha256=legacy.sha(state_path),
                    monitor_failures=state.get('monitor_failures', []),
                    cleanup_errors=state.get('cleanup_errors', []),
                    final_summary_sha256=summary_sha,
                    final_monitor_barrier=final_barrier,
                    final_monitor_errors=final_errors,
                    note='Final sampler join reviewed; original raw state/seal remain unchanged.'))
                if failed:
                    raise legacy.EnvironmentAbort('final telemetry/cleanup failure')


if __name__ == '__main__':
    main()

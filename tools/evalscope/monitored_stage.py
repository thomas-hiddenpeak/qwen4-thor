"""Finalization contract for frozen stages using the suffix telemetry sampler.

This does not acquire a GPU lease or accept performance. The stage driver must
still require the outer gpu_exclusive lease and enforce its frozen plan.
"""
import hashlib
import json
import math
from pathlib import Path
import time


class MonitorIncomplete(RuntimeError):
    """Monitoring evidence cannot support a measured stage or a decision."""


def _read(path):
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('expected a JSON object: ' + str(path))
    return value, hashlib.sha256(raw).hexdigest()


def _time(value):
    if (type(value) not in (int, float) or not math.isfinite(value) or
            value < 0):
        raise ValueError('invalid monitor timestamp/duration')
    return value


def _summary_errors(summary, barrier, require_barrier):
    errors = []
    try:
        attempts, flushed = (summary[k] for k in
                             ('sample_attempts', 'flushed_samples'))
        if (summary['sampler_status'] != 'STOPPED' or
                summary['sampler_failure'] is not None or
                summary['failures_at_exit'] != [] or
                type(attempts) is not int or type(flushed) is not int or
                not attempts == flushed > 0 or
                type(summary['unflushed_attempts']) is not int or
                summary['unflushed_attempts'] != 0 or
                type(summary['exit_pending']['active']) is not int or
                summary['exit_pending']['active'] != 0):
            errors.append('final sampler summary is incomplete/failed')
        if require_barrier:
            boundary = _time(barrier['boundary_monotonic'])
            started = _time(barrier['sample_started_monotonic'])
            flushed_at = _time(barrier['flushed_monotonic'])
            last = _time(summary['last_flushed_heartbeat'])
            duration = _time(summary['final_successful_cycle_duration_seconds'])
            if not (boundary <= started <= flushed_at <= last and
                    last - duration >= boundary):
                errors.append('final summary lacks post-shutdown flush')
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        errors.append('invalid final sampler evidence: ' + repr(error))
    return errors


def require_complete_monitor(plan, postflight_path=None):
    """Read-only gate before analysis; never rewrites historical decisions.

    A valid result means only that stage monitoring finished. HTTP/numerical
    checks, source identity, the outer lease and performance remain separate.
    """
    output = Path(plan['output'])
    path = (Path(postflight_path) if postflight_path is not None else
            output.parent / 'postflight.json')
    try:
        state, state_sha = _read(output / 'state.json')
        summary, summary_sha = _read(output / 'telemetry-summary.json')
        post, _ = _read(path)
        valid = (state['status'] == 'MEASURED_REQUIRES_REVIEW' and
                 state['monitor_failures'] == [] and
                 state.get('cleanup_errors', []) == [] and
                 post['status'] == 'MEASURED_REQUIRES_REVIEW' and
                 post['monitor_failures'] == [] and
                 post['cleanup_errors'] == [] and
                 post['final_monitor_errors'] == [] and
                 post['state_sha256'] == state_sha and
                 post['final_summary_sha256'] == summary_sha and
                 not _summary_errors(summary, post['final_monitor_barrier'],
                                     require_barrier=True))
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as error:
        raise MonitorIncomplete('missing/invalid final monitoring evidence') from error
    if not valid:
        raise MonitorIncomplete('monitor finalization failed; no decision allowed')
    return post


def _wait_for_sample(sampler, failures, heartbeat, maximum_age, deadline):
    boundary = time.monotonic()
    end = min(deadline, boundary + _time(maximum_age))
    while True:
        # The absolute stage budget also bounds finalization. A late sample
        # must not turn an exhausted plan into a successful collection.
        if time.monotonic() >= end:
            raise MonitorIncomplete('no final post-shutdown monitor flush in budget')
        if failures or not sampler.is_alive():
            raise MonitorIncomplete('final monitor failed/stopped: ' + repr(failures))
        view = dict(heartbeat)
        if ('last_sample' in view and 'last_cycle_duration_seconds' in view):
            flushed = _time(view['last_sample'])
            duration = _time(view['last_cycle_duration_seconds'])
            started = flushed - duration
            if started >= boundary:
                return dict(boundary_monotonic=boundary,
                            sample_started_monotonic=started,
                            flushed_monotonic=flushed)
        time.sleep(min(.05, max(0, end - time.monotonic())))


def _postflight(output, barrier):
    state, state_sha, summary_sha, errors = {}, None, None, []
    try:
        state, state_sha = _read(output / 'state.json')
        if (state.get('status') not in ('MEASURED_REQUIRES_REVIEW', 'STOPPED',
                                        'ENV_ABORT') or
                not isinstance(state.get('monitor_failures'), list) or
                not isinstance(state.get('cleanup_errors', []), list)):
            errors.append('missing/invalid final stage status')
    except (OSError, ValueError, TypeError) as error:
        errors.append('final stage state: ' + repr(error))
    try:
        summary, summary_sha = _read(output / 'telemetry-summary.json')
        errors.extend(_summary_errors(summary, barrier,
            require_barrier=state.get('status') == 'MEASURED_REQUIRES_REVIEW'))
    except (OSError, ValueError, TypeError) as error:
        errors.append('final sampler summary: ' + repr(error))
    failed = bool(errors or state.get('monitor_failures') or
                  state.get('cleanup_errors') or state.get('status') == 'ENV_ABORT')
    return dict(status='ENV_ABORT' if failed else state['status'],
                state_sha256=state_sha,
                monitor_failures=state.get('monitor_failures', []),
                cleanup_errors=state.get('cleanup_errors', []),
                final_summary_sha256=summary_sha,
                final_monitor_barrier=barrier,
                final_monitor_errors=errors,
                note='Final sampler join reviewed; raw state/seal unchanged.')


def run_stage(driver, telemetry, plan, plan_path, *, audit_server=None):
    """Run, wait for a fresh complete sample, join, then check final evidence.

    Only the sampler and server auditor are temporarily adapted; all original
    callbacks are restored even on failure. This adapter is process-local and
    must not be used for concurrent stages in the same Python process.
    """
    output = Path(plan['output'])
    receipt = Path(plan_path).resolve().parent / 'postflight.json'
    if output.exists() or receipt.exists():
        raise FileExistsError('refuse to reuse a stage output or postflight receipt')
    if receipt.is_relative_to(output.resolve()):
        raise ValueError('postflight receipt must be outside the sealed output')
    driver.require_guard()
    telemetry.configure_sources(plan['telemetry_sources'])
    original_sample, original_audit = driver.sample_memory, driver.audit_server
    barrier = {}

    def finish(sampler, failures, heartbeat, deadline):
        try:
            barrier.update(_wait_for_sample(sampler, failures, heartbeat,
                plan['resource_sample_max_age_seconds'], deadline))
        except (MonitorIncomplete, ValueError) as error:
            raise driver.EnvironmentAbort(str(error)) from error

    driver.sample_memory = telemetry.sample
    if audit_server is not None:
        driver.audit_server = audit_server
    try:
        try:
            driver.run_stage(plan, Path(plan_path).resolve(), final_monitor=finish)
        finally:
            post = _postflight(output, barrier)
            try:
                with receipt.open('x') as stream:
                    json.dump(post, stream, ensure_ascii=False, indent=2)
                    stream.write('\n')
            except OSError as error:
                raise driver.EnvironmentAbort('cannot save final monitoring receipt') from error
            if post['status'] == 'ENV_ABORT':
                raise driver.EnvironmentAbort('final telemetry/cleanup failure')
        try:
            return require_complete_monitor(plan, receipt)
        except MonitorIncomplete as error:
            raise driver.EnvironmentAbort(str(error)) from error
    finally:
        driver.sample_memory, driver.audit_server = original_sample, original_audit

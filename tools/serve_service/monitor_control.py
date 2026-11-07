"""Explicit one-shot monitor rearm; no model launch, signal or restart API."""
from pathlib import Path
import secrets
import time

from tools.serve_service import package as pkg, supervisor

PROFILE = 'nvml-service-degraded-v3'
RESUME_LIMIT = 2
COOLDOWN_SECONDS = 60
REQUEST_NAME = 'monitor.resume.request.json'


def denial(state, now, *, stopping=False):
    if stopping or state.get('closed'):
        return 'STOPPING'
    if state.get('fatal'):
        return 'FATAL_FAULT'
    if not state.get('suspended'):
        return 'NOT_SUSPENDED'
    if not state.get('query_worker_reaped'):
        return 'WORKER_UNREAPED'
    if state['resume_attempts'] >= RESUME_LIMIT:
        return 'RESUME_LIMIT'
    if now < state['resume_not_before_monotonic']:
        return 'COOLDOWN'
    return None


def valid_request(value):
    return (type(value) is dict and set(value) ==
            {'version', 'request_id', 'generation', 'suspension_epoch'} and
            type(value['version']) is int and value['version'] == 1 and
            all(type(value[k]) is str and len(value[k]) == 32 and
                all(c in '0123456789abcdef' for c in value[k])
                for k in ('request_id', 'generation')) and
            type(value['suspension_epoch']) is int and
            value['suspension_epoch'] > 0)


def request(output):
    """Submit intent only; the owning monitor rechecks every condition."""
    output = Path(output).resolve(strict=True)
    control = pkg.read(output/'controller.json')
    pkg.require(supervisor.process_identity(control['owner']['pid']) ==
                control['owner'], 'monitor controller identity changed')
    manifest = pkg.verify(control['package'], control['manifest_sha256'])
    pkg.require(manifest['version'] in (8, 9, 10) and
                manifest['settings'].get('nvml_policy') == 'degrade' and
                manifest['settings']['restart_limit'] == 0,
                'monitor resume requires a v8/v9/v10 degrade package without restart')
    state = pkg.read(output/'lease/compute-monitor.json')
    expected = 'nvml-service-degraded-v2' if manifest['version'] == 8 else PROFILE
    pkg.require(state.get('policy') == expected, 'monitor profile mismatch')
    reason = denial(state, time.monotonic(), stopping=
                    (output/'stop.request.json').exists() or
                    (output/'terminal.json').exists())
    pkg.require(reason is None, 'monitor resume rejected: '+str(reason))
    value = dict(version=1, request_id=secrets.token_hex(16),
                 generation=state['generation'],
                 suspension_epoch=state['suspension_epoch'])
    # Atomic private mailbox. Simultaneous submissions can coalesce; the
    # generation/epoch check permits at most one execution for that suspension.
    pkg.save(output/REQUEST_NAME, value)
    return dict(state='MONITOR_RESUME_REQUESTED', **value,
                monitoring_restored=False, model_restart_requested=False)

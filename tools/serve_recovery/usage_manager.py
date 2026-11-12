"""Private quota handoff diagnostics and explicit STOPPED legacy migration."""
import fcntl
import hashlib
import os
from pathlib import Path
import time

from tools.serve_recovery import access_store, ledger, model_admin, quota_control
from tools.serve_recovery import store, usage_control, usage_policy, usage_store


def failure(error):
    code = ('usage_writer_busy' if isinstance(error, BlockingIOError) else
            'usage_io_unavailable' if isinstance(error, OSError) else 'usage_state_rejected')
    return dict(status='FAILED', category=type(error).__name__, code=code,
                commit_state='UNKNOWN', model_actions=False)


def migrate(state, desc, manifest):
    ledger.require(manifest['version'] < 8 and manifest.get('ingress_mode') == 'native',
                   'only legacy native profiles require usage migration')
    state = Path(state)
    fd = os.open(state/'guardian.lock', os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        held = store.check_file(fd);fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        named = (state/'guardian.lock').stat(follow_symlinks=False)
        ledger.require((held.st_dev, held.st_ino) == (named.st_dev, named.st_ino),
                       'guardian ownership lock changed')
        with store.Store(state, desc['service_id']) as core:
            ledger.require(core.state['desired'] == 'STOPPED' and not any(core.state['active'].values()),
                           'usage migration requires durable STOPPED')
            return usage_store.initialize_bound(access_store.binding(state, desc['service_id']))
    finally:os.close(fd)


def live(desc, manifest):
    before = model_admin.observe(desc, manifest)
    generation = before['model_generation']
    channel = usage_control.Channel(quota_control.address(desc['service_id'], generation))
    try:
        deadline = time.monotonic()+1
        while time.monotonic()<deadline:
            result = channel.step(before['model_pid'], before['model_start_ticks'])
            if result['applied']:
                after = model_admin.recheck(desc, before)
                value = result['observed']
                return dict(recovered=value['recovered'], frozen=value['frozen'],
                    receipt_sha256=value['receipt_sha256'], sealed_snapshot=value['snapshot'] is not None,
                    model_generation=generation, model_pid=after['model_pid'],
                    model_start_ticks=after['model_start_ticks'], quota_wait_ms=after['quota_wait_ms'])
            time.sleep(.01)
        raise TimeoutError('usage observation deadline')
    finally:channel.close()


def status(state, desc, manifest):
    result = dict(durable=None, durable_error='', live=None, live_error='',
                  continuity='unknown', model_actions=False)
    durable = None
    try:
        with usage_store.open_bound(access_store.binding(state, desc['service_id'])) as writer:
            durable = writer.head['state']['policy']
            result['durable'] = usage_policy.describe(durable)
            result['durable']['snapshot_kind'] = ('closed_generation' if durable['phase'] == 'sealed'
                else 'restore_source' if durable['snapshot'] is not None else 'none')
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        result['durable_error'] = failure(error)['code']
    try:result['live'] = live(desc, manifest)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):result['live_error'] = 'usage_live_unknown'
    value = result['live']
    if value and value['recovered'] and not value['frozen']:
        if value['receipt_sha256'] == hashlib.sha256(b'null').hexdigest():
            result['continuity'] = 'conservative_new_window'
        elif (durable and durable['phase'] == 'active' and
                durable['generation'] == value['model_generation'] and
                durable['owner']['pid'] == value['model_pid'] and
                durable['owner']['start_ticks'] == value['model_start_ticks']):
            try:
                boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
                plan = usage_policy.restoration(durable, boot, time.monotonic_ns())
                if value['receipt_sha256'] == hashlib.sha256(ledger.canonical(plan['snapshot'])).hexdigest():
                    result['continuity'] = plan['mode']
            except (OSError, ValueError, KeyError, TypeError, RuntimeError):pass
    return result

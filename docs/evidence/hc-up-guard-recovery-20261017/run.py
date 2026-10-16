"""Fixed three CPU integration cases; no real GPU configuration IO."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import time

out = Path(__file__).resolve().parent
results = []
for mode in ('normal', 'kill', 'restore_failure'):
    d = out / mode
    d.mkdir()
    (d / 'fake-clock').write_text('original\n')
    unit = f'q4t-guard-recovery-{os.getpid()}-{mode}.service'
    cmd = ['sudo', '-n', 'systemd-run', '--unit=' + unit, '--wait', '--collect',
           '--property=User=' + str(os.getuid()),
           '--property=Group=' + str(os.getgid()), '--property=Type=exec',
           '--property=NotifyAccess=main', '--property=FileDescriptorStoreMax=1',
           '--property=FileDescriptorStorePreserve=restart',
           '--property=RuntimeMaxSec=15', '--property=TimeoutStopSec=infinity',
           '--property=KillMode=control-group',
           '--property=ExecStopPost=/usr/bin/python3 ' + str(out / 'probe.py') +
           ' cleanup ' + mode + ' ' + str(d) + ' ' + unit,
           '/usr/bin/python3', str(out / 'probe.py'), 'main', mode, str(d), unit]
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True)
    failed_restore_held = None
    try:
        if mode == 'restore_failure':
            deadline = time.monotonic() + 20
            while not (d / 'restore-failed.json').exists():
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError('failed restore phase did not appear')
                time.sleep(0.05)
            failed_restore_held = False
            with (d / 'lease').open('r+') as probe:
                try:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    failed_restore_held = True
            assert failed_restore_held
            assert (d / 'fake-clock').read_text() == 'lower\n'
            state = subprocess.check_output(
                ['systemctl', 'show', unit, '--property=SubState', '--value'],
                text=True).strip()
            assert state == 'stop-post', state
            (d / 'repair-gate.json').write_text(json.dumps(
                {'lease_held': failed_restore_held, 'substate': state,
                 'process_alive': process.poll() is None}, indent=2) + '\n')
            (d / 'repair-authorized').touch()
        stdout, stderr = process.communicate(timeout=25)
    finally:
        # Only a fake-state probe. Ensure an assertion cannot strand its service.
        (d / 'repair-authorized').touch()
        if process.poll() is None:
            process.communicate(timeout=25)
    (d / 'command-result.json').write_text(json.dumps(
        {'command': cmd, 'returncode': process.returncode,
         'stdout': stdout, 'stderr': stderr}, indent=2) + '\n')
    cleanup = json.loads((d / 'cleanup.json').read_text())
    assert cleanup['lease_still_held'] and cleanup['after'] == 'original\n'
    assert (process.returncode == 0) == (mode != 'kill')
    if mode != 'kill':
        status = json.loads((d / 'guard/exit.json').read_text())
        assert status['status'] == 'COMPLETED' and status['external_lease']
        events = (d / 'guard/events.jsonl').read_text()
        assert 'lease_guard_finished' in events and 'lease_released' not in events
    with (d / 'lease').open('r+') as probe:
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    load_state = subprocess.check_output(
        ['systemctl', 'show', unit, '--property=LoadState', '--value'],
        text=True).strip()
    assert load_state == 'not-found', load_state
    results.append({'mode': mode, 'returncode': process.returncode,
                    'cleanup': cleanup, 'failed_restore_held': failed_restore_held,
                    'released_after_restore': True, 'unit_load_state': load_state})
(out / 'result.json').write_text(json.dumps(results, indent=2) + '\n')
print(json.dumps(results, indent=2))

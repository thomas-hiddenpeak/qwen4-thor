"""Fixed four CPU integration cases; no real GPU configuration IO."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import time
import sys

out = Path(__file__).resolve().parent
sys.path.insert(0,str(out.parents[1]/'tools/evalscope'))
import gpu_exclusive as guard
from gpu_clock_journal import ClockJournal
results = []
for mode in ('normal', 'kill', 'restore_failure', 'cleanup_kill'):
    d = out / mode
    d.mkdir()
    (d / 'fake-clock').write_text('[1575,1575]\n')
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
            assert (d / 'fake-clock').read_text() == '[1260, 1260]\n'
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
    if mode == 'cleanup_kill':
        killed = json.loads((d / 'cleanup-kill.json').read_text())
        assert killed['lease_still_held'] and killed['before'] == [1260,1260]
        assert process.returncode != 0
        guard.inspect = lambda *args: (_ for _ in ()).throw(AssertionError('must not inspect'))
        rc = guard.run_guard(['must-not-run'],d/'refused',d/'lease')
        assert rc == 75
        assert json.loads((d/'refused/exit.json').read_text())['status'] == 'RECOVERY_REQUIRED'
        # Simulated explicit repair under a freshly acquired lease.
        with (d/'lease').open('r+') as held:
            fcntl.flock(held,fcntl.LOCK_EX | fcntl.LOCK_NB)
            def read():return json.loads((d/'fake-clock').read_text())
            def write(field,value):
                values=read();values[0 if field=='min_freq' else 1]=value
                (d/'fake-clock').write_text(json.dumps(values)+'\n')
            ClockJournal(d/'lease.recovery-required',read,write,
                         {'boot':'fake-boot','device':'fake-device'},[1260,1575]).restore()
        assert read() == [1575,1575] and not (d/'lease.recovery-required').exists()
        (d/'manual-repair.json').write_text(json.dumps({'guard_refused':True,'restored':read()})+'\n')
        cleanup = {'after':read(),'lease_still_held':True,'manual_recovery':True}
    else:
        cleanup = json.loads((d / 'cleanup.json').read_text())
    assert cleanup['lease_still_held'] and cleanup['after'] == [1575,1575]
    assert (process.returncode == 0) == (mode not in ('kill','cleanup_kill'))
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
                    'released_after_restore': mode != 'cleanup_kill',
                    'fallback_marker_required': mode == 'cleanup_kill', 'unit_load_state': load_state})
(out / 'result.json').write_text(json.dumps(results, indent=2) + '\n')
print(json.dumps(results, indent=2))

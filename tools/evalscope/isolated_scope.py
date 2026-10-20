"""A memcg payload retaining caller ancestry for the repository GPU lease.

Scope helper charge is included, clients stay outside. MemoryMax is not a
physical RAM limit. Only a unit bearing our random token may be stopped.
"""
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import time

from isolated_service import PROPERTIES, unit_properties
from resource_metrics import collect_resources

SCOPE_PATTERN = re.compile(r'q4t-[A-Za-z0-9][A-Za-z0-9_.-]*\.scope\Z')


def scope_properties(unit):
    result = subprocess.run(['sudo', '-n', 'systemctl', 'show', unit] +
        ['--property=' + key for key in (*PROPERTIES, 'Description', 'OOMPolicy')],
        capture_output=True, text=True, timeout=15)
    values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    if not values or 'LoadState' not in values:
        raise RuntimeError('scope properties unavailable: ' + result.stderr)
    return values


def stop_owned_scope(unit, token):
    if not SCOPE_PATTERN.fullmatch(unit) or not re.fullmatch(r'q4t-scope-[0-9a-f]{32}', token):
        raise ValueError('invalid scope ownership identity')
    props = scope_properties(unit)
    stopped = reset = None
    if props['LoadState'] != 'not-found':
        if props.get('Description') != token:
            raise RuntimeError('refusing cleanup of another scope owner')
        stopped = subprocess.run(['sudo', '-n', 'systemctl', 'stop', unit],
            capture_output=True, text=True, timeout=40)
        if stopped.returncode:
            raise RuntimeError('scope stop failed: ' + stopped.stderr)
        reset = subprocess.run(['sudo', '-n', 'systemctl', 'reset-failed', unit],
            capture_output=True, text=True, timeout=15)
    after = scope_properties(unit)
    if after['LoadState'] != 'not-found':
        raise RuntimeError('scope cleanup incomplete')
    return dict(stop_rc=stopped.returncode if stopped else None,
        reset_rc=reset.returncode if reset else None,
        already_absent=stopped is None, properties_after=after, unit_removed=True)


class IsolatedScope:
    def __init__(self, command, *, cwd, env, log_path, unit, memory_max, evidence):
        if not SCOPE_PATTERN.fullmatch(unit):
            raise ValueError('unit must be a unique q4t-*.scope name')
        if memory_max is not None and memory_max <= 0:
            raise ValueError('host/cache maximum must be positive')
        self.unit = unit
        self.evidence = Path(evidence).resolve()
        self.pid, self.pidfd, self.launcher = 0, None, None
        self.cgroup, self.returncode = None, None
        self.closed, self.owned = False, False
        self.token = 'q4t-scope-' + secrets.token_hex(16)
        if unit_properties(unit).get('LoadState') != 'not-found':
            raise RuntimeError('refusing to reuse an existing unit: ' + unit)
        self.evidence.mkdir(exist_ok=False)
        self._save('spec.json', dict(command=list(map(str, command)),
            cwd=str(Path(cwd).resolve()), log_path=str(Path(log_path).resolve()),
            env={k: v for k, v in env.items() if k.startswith('Q4T_') or
                 k in ('PATH', 'LD_LIBRARY_PATH', 'LANG', 'LC_ALL')}, token=self.token))
        launch = ['sudo', '-n', 'systemd-run', '--quiet', '--scope', '--unit=' + unit,
            '--description=' + self.token, '--property=MemoryAccounting=yes',
            '--property=IOAccounting=yes', '--property=MemorySwapMax=0',
            '--property=OOMPolicy=continue',
            '--property=MemoryMax=' + str(memory_max or 'infinity'),
            '--property=TasksMax=4096', '--', '/usr/bin/setpriv',
            '--reuid=' + str(os.getuid()), '--regid=' + str(os.getgid()),
            '--init-groups', '/usr/bin/python3',
            str(Path(__file__).with_name('scope_payload.py')), str(self.evidence)]
        self._save('launch.json', dict(argv=launch, unit=unit,
            host_cache_max_bytes=memory_max, swap_max_bytes=0,
            scope='memcg charge including scope helper, excluding clients; not total physical RAM',
            exit_counter_retention='helper retains scope for up to 15 seconds'))
        self.log = (self.evidence / 'launcher.log').open('w')
        try:
            self.launcher = subprocess.Popen(launch, stdout=self.log,
                                             stderr=subprocess.STDOUT)
            deadline = time.monotonic() + 30
            payload = self.evidence / 'payload.json'
            while not payload.exists():
                if self.launcher.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError('scope payload binding failed')
                time.sleep(.05)
            identity = json.loads(payload.read_text())
            props = self._properties()
            if (identity['token'] != self.token or props.get('Description') != self.token or
                    props.get('OOMPolicy') != 'continue'):
                raise RuntimeError('scope ownership token mismatch')
            self.owned = True
            self.pid = identity['pid']
            self.cgroup = props.get('ControlGroup') or None
            self.pidfd = os.pidfd_open(self.pid)
            fields = Path(f'/proc/{self.pid}/stat').read_text().rsplit(')', 1)[1].split()
            membership = Path(f'/proc/{self.pid}/cgroup').read_text().splitlines()
            if (int(fields[19]) != identity['start_ticks'] or not self.cgroup or
                    '0::' + self.cgroup not in membership):
                raise RuntimeError('scope PID/cgroup identity mismatch')
            self._save('identity.json', dict(kind='scope', token=self.token, pid=self.pid, properties=props,
                membership=membership, start_ticks=identity['start_ticks']))
            self.snapshot('bound')
        except BaseException:
            self.close()
            raise

    def _save(self, name, value):
        with (self.evidence / name).open('x') as f:
            json.dump(value, f, indent=2)
            f.write('\n')

    def _properties(self):
        return scope_properties(self.unit)

    def snapshot(self, label):
        props = self._properties()
        if props.get('LoadState') != 'not-found' and props.get('Description') != self.token:
            raise RuntimeError('scope replaced by another owner')
        row = dict(label=label, t=time.time(), monotonic=time.monotonic(), properties=props,
            resources=collect_resources(self.pid or None, expected_cgroup=self.cgroup))
        with (self.evidence / 'endpoints.jsonl').open('a') as f:
            f.write(json.dumps(row) + '\n')
        return row

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        path = self.evidence / 'payload-exit.json'
        if path.exists():
            value = json.loads(path.read_text())
            if value['token'] != self.token:
                raise RuntimeError('scope exit token mismatch')
            self.returncode = value['returncode']
        elif self.launcher.poll() is not None:
            raise RuntimeError('scope exited without payload exit evidence')
        return self.returncode

    def _signal(self, sig):
        if self.poll() is None:
            signal.pidfd_send_signal(self.pidfd, sig)

    def terminate(self):
        self.snapshot('before_terminate')
        self._signal(signal.SIGTERM)

    def kill(self):
        self._signal(signal.SIGKILL)

    def wait(self, timeout=None):
        deadline = time.monotonic() + timeout if timeout is not None else float('inf')
        while self.poll() is None:
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.unit, timeout)
            time.sleep(.1)
        return self.returncode

    def close(self):
        if self.closed:
            return
        try:
            props = self._properties()
            if props.get('LoadState') != 'not-found':
                if props.get('Description') != self.token:
                    raise RuntimeError('refusing cleanup of another scope owner')
                self.owned = True
                self.snapshot('before_unit_cleanup')
            cleanup = stop_owned_scope(self.unit, self.token)
            if self.launcher is not None:
                self.launcher.wait(timeout=20)
            self.closed = cleanup['unit_removed']
            self._save('cleanup.json', cleanup)
        finally:
            if self.pidfd is not None:
                os.close(self.pidfd)
                self.pidfd = None
            self.log.close()

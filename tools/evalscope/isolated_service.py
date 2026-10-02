"""Own one systemd service without charging its client to the experiment.

MemoryMax applies to charged host/cache pages, not all Thor CUDA allocations.
An ExecStopPost sleep retains the cgroup briefly for final counter collection.
Only the unique unit created by this object is ever stopped or reset.
"""
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import time


UNIT_PATTERN = re.compile(r'q4t-[A-Za-z0-9][A-Za-z0-9_.-]*\.service\Z')
PROPERTIES = ('LoadState', 'ActiveState', 'SubState', 'MainPID', 'ControlPID', 'ControlGroup',
              'ExecMainCode', 'ExecMainStatus', 'Result', 'MemoryMax',
              'MemorySwapMax', 'MemoryAccounting', 'IOAccounting')


def unit_properties(unit):
    result = subprocess.run(
        ['sudo', '-n', 'systemctl', 'show', unit] +
        ['--property=' + name for name in PROPERTIES],
        capture_output=True, text=True, timeout=15)
    values = dict(line.split('=', 1) for line in result.stdout.splitlines()
                  if '=' in line)
    if result.returncode and not values:
        raise RuntimeError('cannot query experiment unit: ' + result.stderr)
    return values


class IsolatedService:
    def __init__(self, command, *, cwd, env, log_path, unit, memory_max,
                 evidence):
        if not UNIT_PATTERN.fullmatch(unit):
            raise ValueError('unit must be a unique q4t-*.service name')
        if memory_max is not None and memory_max <= 0:
            raise ValueError('host/cache maximum must be positive')
        self.unit = unit
        self.evidence = Path(evidence)
        self.evidence.mkdir(exist_ok=False)
        self.pid = 0
        self.returncode = None
        self.cgroup = None
        self.owned = False
        self.closed = False
        if unit_properties(unit).get('LoadState') != 'not-found':
            raise RuntimeError('refusing to reuse an existing unit: ' + unit)
        log_path = Path(log_path).resolve()
        if any(c in str(log_path) + str(cwd) for c in '\n\r%'):
            raise ValueError('unsupported systemd specifier in experiment path')
        selected_env = {key: value for key, value in env.items()
                        if key.startswith('Q4T_') or key in
                        ('PATH', 'LD_LIBRARY_PATH', 'LANG', 'LC_ALL')}
        launch = ['sudo', '-n', 'systemd-run', '--quiet', '--unit=' + unit,
                  '--uid=' + pwd.getpwuid(os.getuid()).pw_name,
                  '--service-type=exec',
                  '--property=ExecStopPost=/usr/bin/sleep 10',
                  '--property=MemoryAccounting=yes', '--property=IOAccounting=yes',
                  '--property=MemoryMax=' + str(memory_max or 'infinity'),
                  '--property=MemorySwapMax=0', '--property=TasksMax=4096',
                  '--property=TimeoutStopSec=30s',
                  '--property=WorkingDirectory=' + str(Path(cwd).resolve()),
                  '--property=StandardOutput=append:' + str(log_path),
                  '--property=StandardError=inherit']
        launch += ['--setenv=' + key + '=' + value
                   for key, value in sorted(selected_env.items())]
        launch += ['--'] + [str(arg) for arg in command]
        self._save('launch.json', {
            'argv': launch, 'unit': unit, 'host_cache_max_bytes': memory_max,
            'swap_max_bytes': 0,
            'scope': 'memcg charge; excludes incompletely charged CUDA paths '
                     'and externally owned file cache; not total physical RAM',
            'exit_counter_retention': 'ExecStopPost sleep 10; its small host '
                                      'charge is included in final cgroup counters',
        })
        # Claim ownership only after the no-reuse check, before creation so
        # startup errors still get an endpoint and cleanup attempt.
        self.owned = True
        try:
            result = subprocess.run(launch, capture_output=True, text=True,
                                    timeout=30)
            self._save('launch-result.json', {'returncode': result.returncode,
                                             'stdout': result.stdout,
                                             'stderr': result.stderr})
            if result.returncode:
                raise RuntimeError('isolated service launch failed: ' + result.stderr)
            props = unit_properties(self.unit)
            self.pid = int(props.get('MainPID', '0'))
            self.cgroup = props.get('ControlGroup') or None
            if not self.pid or not self.cgroup:
                raise RuntimeError('isolated service exited before PID binding')
            membership = Path(f'/proc/{self.pid}/cgroup').read_text().splitlines()
            if '0::' + self.cgroup not in membership:
                raise RuntimeError('service PID/cgroup identity mismatch')
            self._save('identity.json', {'pid': self.pid, 'properties': props,
                                         'membership': membership})
            self.snapshot('bound')
        except BaseException:
            self.close()
            raise

    def _save(self, name, data):
        with (self.evidence / name).open('x') as stream:
            json.dump(data, stream, indent=2)
            stream.write('\n')

    def snapshot(self, label):
        from resource_metrics import collect_resources
        props = unit_properties(self.unit)
        if not self.cgroup:
            self.cgroup = props.get('ControlGroup') or None
        row = {'label': label, 't': time.time(), 'monotonic': time.monotonic(),
               'properties': props,
               'resources': collect_resources(self.pid or None,
                                               expected_cgroup=self.cgroup)}
        with (self.evidence / 'endpoints.jsonl').open('a') as stream:
            stream.write(json.dumps(row) + '\n')
        return row

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        props = unit_properties(self.unit)
        code = int(props.get('ExecMainCode', '0'))
        status = int(props.get('ExecMainStatus', '0'))
        if code:
            self.returncode = status if code == 1 else -status
        elif props.get('ActiveState') in ('failed', 'inactive'):
            self.returncode = 1
        elif props.get('LoadState') == 'not-found':
            raise RuntimeError('owned service disappeared before exit capture')
        return self.returncode

    def _signal(self, signal):
        if self.poll() is None:
            subprocess.run(['sudo', '-n', 'systemctl', 'kill',
                            '--kill-whom=all', '--signal=' + signal, self.unit],
                           check=True, capture_output=True, timeout=15)

    def terminate(self):
        self.snapshot('before_terminate')
        self._signal('SIGTERM')

    def kill(self):
        self._signal('SIGKILL')

    def wait(self, timeout=None):
        start = time.monotonic()
        while self.poll() is None:
            if timeout is not None and time.monotonic() - start > timeout:
                raise subprocess.TimeoutExpired(self.unit, timeout)
            time.sleep(0.1)
        return self.returncode

    def close(self):
        if self.closed or not self.owned:
            return
        try:
            self.snapshot('before_unit_cleanup')
        finally:
            stopped = subprocess.run(['sudo', '-n', 'systemctl', 'stop', self.unit],
                                     capture_output=True, text=True, timeout=40)
            reset = subprocess.run(['sudo', '-n', 'systemctl', 'reset-failed',
                                    self.unit], capture_output=True, text=True,
                                   timeout=15)
            after = unit_properties(self.unit)
            clean = after.get('LoadState') == 'not-found'
            self._save('cleanup.json', {
                'stop_rc': stopped.returncode, 'stop_stderr': stopped.stderr,
                'reset_rc': reset.returncode, 'reset_stderr': reset.stderr,
                'properties_after': after, 'unit_removed': clean,
            })
            self.closed = clean
            if not clean:
                raise RuntimeError('experiment unit cleanup incomplete: ' + self.unit)

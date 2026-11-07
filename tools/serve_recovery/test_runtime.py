"""Real CPU Guardian lifecycle; private test locks, no models or GPU queries."""
import argparse
import ctypes
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import control, journal, store

BINARY = Path(os.environ.get('Q4T_GUARDIAN_BINARY', ROOT/'build/q4t-guardian'))


def children(pid):
    # Thor's kernel does not expose task/children. Inspect actual PPIDs, as
    # the native subreaper does, rather than requiring that optional file.
    result = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            fields = (entry/'stat').read_text().rsplit(')', 1)[1].split()
        except (FileNotFoundError, ProcessLookupError):
            continue
        if int(fields[1]) == pid:
            result.append(int(entry.name))
    return result


def signal_identity(pid, ticks, signum):
    fd = os.pidfd_open(pid)
    try:
        if journal.process_start(pid) != ticks:
            raise RuntimeError('fixture PID identity changed')
        signal.pidfd_send_signal(fd, signum)
    finally:
        os.close(fd)


class Service:
    def __init__(self, output, *, escape=False):
        self.directory = Path(tempfile.mkdtemp(prefix='service-', dir=output))
        self.service_id = str(uuid.uuid4())
        with store.Store(self.directory, self.service_id, initialize=True):
            pass
        self.lease_path = self.directory/'private-cpu-lease'
        lease = os.open(self.lease_path, os.O_RDWR | os.O_CREAT | os.O_EXCL,
                        0o600)
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        command = [sys.executable, '-I', '-B',
                   str(ROOT/'tools/serve_recovery/fixture_controller.py')]
        if escape:
            command.append('--escape-child')
        self.config = dict(version=1, directory=str(self.directory),
            service_id=self.service_id, python=sys.executable,
            journal=str(ROOT/'tools/serve_recovery/journal.py'),
            controller=command, lease_path=str(self.lease_path), lease_fd=lease,
            resource_mode='process', require_hard_memory_limit=False,
            term_ms=100, kill_ms=200, heartbeat_ms=1000, critical_memory_mib=64)
        self.config_path = self.directory/'config.json'
        self.config_path.write_text(json.dumps(self.config))
        self.config_path.chmod(0o600)
        self.log = (self.directory/'native.log').open('wb')
        try:
            self.process = subprocess.Popen([str(BINARY), '--config',
                str(self.config_path), '--activate'], pass_fds=(lease,),
                stdin=subprocess.DEVNULL, stdout=self.log, stderr=self.log,
                env=dict(PATH='/q4t-test-no-system-tools', LANG='C.UTF-8'))
        finally:
            os.close(lease)
        self.snapshots = (self.directory/'observations.jsonl').open('w')

    def status(self):
        value = control.request(self.service_id, 'status')
        self.snapshots.write(json.dumps(value)+'\n')
        self.snapshots.flush()
        return value

    def wait(self, predicate, timeout=10):
        end = time.monotonic()+timeout
        latest = None
        while time.monotonic() < end:
            if self.process.poll() is not None:
                raise AssertionError('Guardian exited: '+
                                     (self.directory/'native.log').read_text())
            try:
                latest = self.status()
            except (FileNotFoundError, ConnectionRefusedError):
                pass
            else:
                if predicate(latest):
                    return latest
            time.sleep(0.02)
        raise AssertionError(f'lifecycle deadline expired: {latest}')

    def ready(self, generation=1):
        return self.wait(lambda s: s['controller_ready'] and
                         s['controller_generation'] == generation)

    def stop(self):
        value = control.request(self.service_id, 'stop', timeout=6)
        if value['cleanup_confirmed']:
            if self.process.wait(timeout=3) != 0:
                raise AssertionError('clean stop returned failure')
        return value

    def persistent(self):
        return json.loads((self.directory/store.STATE).read_text())['state']

    def assert_lease_free(self):
        fd = os.open(self.lease_path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)

    def close(self):
        if self.process.poll() is None:
            try:
                self.stop()
            except (OSError, AssertionError):
                pass
        if self.process.poll() is None:
            self.process.kill()  # Only this unreaped CPU fixture child.
            self.process.wait(timeout=3)
        self.log.close()
        self.snapshots.close()


class RuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.libc = ctypes.CDLL(None, use_errno=True)
        cls.old = ctypes.c_int()
        if cls.libc.prctl(37, ctypes.byref(cls.old), 0, 0, 0) or \
                cls.libc.prctl(36, 1, 0, 0, 0):
            raise RuntimeError('CPU fixture requires subreaper')

    @classmethod
    def tearDownClass(cls):
        cls.libc.prctl(36, cls.old.value, 0, 0, 0)

    def setUp(self):
        self.output = Path(os.environ.get('Q4T_TEST_OUTPUT_DIR',
                                         ROOT/'build/recovery-runtime-tests'))
        self.output.mkdir(parents=True, exist_ok=True)
        self.services = []

    def tearDown(self):
        for service in self.services:
            service.close()
        # Reap only this test runner's CPU descendants, including deliberate
        # escaped orphans. Never inspect or signal arbitrary service PIDs.
        end = time.monotonic()+3
        while time.monotonic() < end:
            live = children(os.getpid())
            if not live:
                return
            for pid in live:
                try:
                    signal_identity(pid, journal.process_start(pid), signal.SIGKILL)
                    os.waitpid(pid, os.WNOHANG)
                except (ProcessLookupError, FileNotFoundError, ChildProcessError):
                    pass
            time.sleep(0.01)
        self.fail('CPU fixture descendants did not exit')

    def launch(self, **kwargs):
        service = Service(self.output, **kwargs)
        self.services.append(service)
        return service

    def test_foreground_control_stop_and_lease_release(self):
        service = self.launch()
        current = service.ready()
        self.assertFalse(current['business_ready'])
        self.assertEqual(current['guardian_pid'], service.process.pid)
        result = service.stop()
        self.assertEqual(result['state'], 'STOPPED')
        self.assertTrue(result['stop_durable'] and result['cleanup_confirmed'])
        self.assertEqual(service.persistent()['desired'], 'STOPPED')
        self.assertTrue(all(value is None for value in
                            service.persistent()['active'].values()))
        service.assert_lease_free()

    def test_controller_crash_cleans_escaped_descendants_before_replacement(self):
        service = self.launch(escape=True)
        first = service.ready()
        escaped = set(children(service.process.pid)) - {
            first['controller_pid'], first['journal_pid']}
        self.assertTrue(escaped, 'escaping fixture did not create an orphan')
        signal_identity(first['controller_pid'], first['controller_start_ticks'],
                        signal.SIGKILL)
        second = service.ready(2)
        self.assertNotEqual(second['controller_pid'], first['controller_pid'])
        self.assertFalse(Path(f"/proc/{first['controller_pid']}").exists())
        for pid in escaped:
            self.assertFalse(Path(f'/proc/{pid}').exists())
        self.assertEqual(len(service.persistent()['attempts']['controller']), 1)
        self.assertEqual(service.stop()['state'], 'STOPPED')
        service.assert_lease_free()

    def test_controller_heartbeat_loss_is_bounded_and_old_child_reaped(self):
        service = self.launch()
        first = service.ready()
        signal_identity(first['controller_pid'], first['controller_start_ticks'],
                        signal.SIGSTOP)
        second = service.ready(2)
        self.assertNotEqual(first['controller_pid'], second['controller_pid'])
        self.assertFalse(Path(f"/proc/{first['controller_pid']}").exists())
        self.assertEqual(service.stop()['state'], 'STOPPED')

    def test_stop_wins_during_controller_recovery(self):
        service = self.launch()
        first = service.ready()
        signal_identity(first['controller_pid'], first['controller_start_ticks'],
                        signal.SIGKILL)
        self.assertEqual(service.stop()['state'], 'STOPPED')
        self.assertEqual(service.persistent()['generations']['controller'], 1)
        service.assert_lease_free()

    def test_sigint_is_a_durable_normal_stop(self):
        service = self.launch()
        service.ready()
        service.process.send_signal(signal.SIGINT)
        self.assertEqual(service.process.wait(timeout=5), 0)
        state = service.persistent()
        self.assertEqual(state['desired'], 'STOPPED')
        self.assertTrue(all(value is None for value in state['active'].values()))
        service.assert_lease_free()

    def test_concurrent_start_rejected_without_disturbing_owner(self):
        service = self.launch()
        first = service.ready()
        fd = os.open(service.lease_path, os.O_RDWR)
        try:
            config = dict(service.config, lease_fd=fd)
            path = service.directory/'duplicate.json'
            path.write_text(json.dumps(config)); path.chmod(0o600)
            with (service.directory/'duplicate.log').open('wb') as log:
                second = subprocess.run([str(BINARY), '--config', str(path),
                    '--activate'], pass_fds=(fd,), stdout=log, stderr=log,
                    timeout=5)
            self.assertNotEqual(second.returncode, 0)
        finally:
            os.close(fd)
        self.assertEqual(service.status()['controller_pid'], first['controller_pid'])
        self.assertEqual(service.stop()['state'], 'STOPPED')

    def test_malformed_local_request_does_not_change_lifecycle(self):
        service = self.launch()
        first = service.ready()
        for payload in (b'{', b' '*9000, b'{"version":1,"op":"stop"}',
                        b'{"version":1,"version":1}'):
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as sock:
                sock.settimeout(2)
                sock.connect(control.address(service.service_id))
                sock.send(payload)
                try:
                    self.assertEqual(sock.recv(8192), b'')
                except ConnectionResetError:
                    pass
        current = service.status()
        self.assertEqual(current['controller_pid'], first['controller_pid'])
        self.assertFalse(current['stop_requested'])
        self.assertEqual(service.stop()['state'], 'STOPPED')

    def test_journal_death_keeps_healthy_controller_and_freezes_replacement(self):
        service = self.launch()
        first = service.ready()
        signal_identity(first['journal_pid'], first['journal_start_ticks'],
                        signal.SIGKILL)
        current = service.wait(lambda s: s['recovery_frozen'] and
                               s['controller_events'] >= first['controller_events']+2)
        self.assertEqual(current['controller_pid'], first['controller_pid'])
        self.assertTrue(current['controller_ready'])
        result = service.stop()
        self.assertFalse(result['stop_durable'])
        self.assertFalse(result['cleanup_confirmed'])
        self.assertIsNone(service.process.poll())
        self.assertEqual(children(service.process.pid), [])
        # Test cleanup may kill the now-empty CPU Guardian. The production
        # path deliberately stays alive instead of claiming a durable stop.

    def test_controller_budget_opens_circuit_without_new_attempt(self):
        service = self.launch()
        current = service.ready()
        for generation in range(2, 5):
            signal_identity(current['controller_pid'], current['controller_start_ticks'],
                            signal.SIGKILL)
            current = service.ready(generation)
        signal_identity(current['controller_pid'], current['controller_start_ticks'],
                        signal.SIGKILL)
        result = service.wait(lambda s: s['state'] == 'CIRCUIT_OPEN' and
                              s['retry_after_ms'] >= 600000)
        self.assertIsNone(result['controller_pid'])
        state = service.persistent()
        self.assertEqual(state['generations']['controller'], 4)
        self.assertEqual(len(state['attempts']['controller']), 3)
        self.assertEqual(service.stop()['state'], 'STOPPED')

    def test_guardian_death_does_not_resurrect_service(self):
        service = self.launch()
        first = service.ready()
        service.process.kill()
        service.process.wait(timeout=3)
        end = time.monotonic()+3
        remaining = {first['controller_pid'], first['journal_pid']}
        while remaining and time.monotonic() < end:
            for pid in list(remaining):
                try:
                    reaped, _status = os.waitpid(pid, os.WNOHANG)
                    if reaped:
                        remaining.remove(pid)
                except ChildProcessError:
                    if not Path(f'/proc/{pid}').exists():
                        remaining.remove(pid)
            time.sleep(0.01)
        self.assertFalse(remaining)
        self.assertEqual(children(os.getpid()), [])
        service.assert_lease_free()
        with self.assertRaises(ConnectionRefusedError):
            control.request(service.service_id, 'status')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--guardian', type=Path, default=BINARY)
    args, rest = parser.parse_known_args()
    BINARY = args.guardian.resolve(strict=True)
    unittest.main(argv=[sys.argv[0], *rest])

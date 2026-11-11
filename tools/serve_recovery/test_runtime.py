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
    def __init__(self, output, *, escape=False, models=False, pause_at=None,
                 device_unknown=False, escape_model=False, bad_ledger=False,
                 stale_grant=False, daemon=False, startup_ms=300000,
                 gateway=False, quota=False, probe_lifecycle=False,
                 probe_only=False, aux_health=False, production=False,
                 usage_handoff=False, quota_restore=None, stop_handoff=False,
                 gateway_diagnostics=False, capacity_fingerprint=None,
                 version_fixture=None):
        self.directory = Path(tempfile.mkdtemp(prefix='service-', dir=output))
        self.service_id = str(uuid.uuid4())
        with store.Store(self.directory, self.service_id, initialize=True):
            pass
        if bad_ledger:
            (self.directory/store.STATE).write_text('{')
        if stale_grant:
            with store.Store(self.directory, self.service_id) as writer:
                for operation, args in [('desired', dict(state='RUNNING')),
                        ('authorize', dict(role='controller', proof=dict(
                            cleanup_complete=True, resources_safe=True,
                            identity_valid=True, lease_continuous=True,
                            device_usable=False)))]:
                    writer.transact(dict(id=str(uuid.uuid4()),
                        expected_revision=writer.state['revision'],
                        operation=operation, args=args))
        self.lease_path = self.directory/'private-cpu-lease'
        lease = os.open(self.lease_path, os.O_RDWR | os.O_CREAT | os.O_EXCL,
                        0o600)
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        command = [sys.executable, '-I', '-B',
                   str(ROOT/'tools/serve_recovery/fixture_controller.py')]
        if escape:
            command.append('--escape-child')
        if models:
            command += ['--model-fixture', str(self.directory)]
        if pause_at:
            command += ['--pause-at', pause_at]
        if device_unknown:
            command.append('--device-unknown')
        if escape_model:
            command.append('--escape-model-child')
        if gateway:
            command.append('--bind-model')
        if capacity_fingerprint is not None:
            command += ['--capacity-fingerprint', capacity_fingerprint]
        if probe_lifecycle:
            command.append('--probe-lifecycle')
        if probe_only:
            command.append('--probe-only')
        if aux_health:
            command.append('--aux-health')
        if quota_restore:
            command += ['--quota-restore', quota_restore]
        if stop_handoff:
            command.append('--stop-handoff')
        if production:
            command = [sys.executable, '-I', '-B',
                str(ROOT/'tools/serve_recovery/fixture_production.py'),
                '--directory', str(self.directory)]
        self.bootstrap = None
        bootstrap_child = None
        if daemon:
            self.bootstrap, bootstrap_child = socket.socketpair(
                socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.config = dict(version=1, directory=str(self.directory),
            service_id=self.service_id, python=sys.executable,
            journal=str(ROOT/'tools/serve_recovery/journal.py'),
            controller=command, lease_path=str(self.lease_path), lease_fd=lease,
            gateway=([sys.executable, '-I', '-B',
                str(ROOT/'tools/serve_recovery/fixture_gateway.py')]
                if gateway else []), quota_enforced=quota,
            resource_mode='process', require_hard_memory_limit=False,
            term_ms=100, kill_ms=200, heartbeat_ms=1000, critical_memory_mib=64,
            launch=dict(mode='daemon' if daemon else 'foreground', parent_pid=os.getpid(),
                parent_start_ticks=journal.process_start(os.getpid()),
                startup_fd=bootstrap_child.fileno() if daemon else 0,
                timeout_ms=startup_ms))
        if usage_handoff:self.config.update(version=2, usage_handoff=True, term_ms=1000)
        if gateway_diagnostics:
            self.config['gateway'] += ['--diagnostics-dir', str(self.directory)]
        if version_fixture is not None:
            self.config.update(version=3,usage_handoff=usage_handoff,
                               version_recovery=version_fixture(self))
        self.config_path = self.directory/'config.json'
        self.config_path.write_text(json.dumps(self.config))
        self.config_path.chmod(0o600)
        self.log = (self.directory/'native.log').open('wb')
        try:
            self.process = subprocess.Popen([str(BINARY), '--config',
                str(self.config_path), '--activate'],
                pass_fds=(lease, bootstrap_child.fileno()) if daemon else (lease,),
                start_new_session=daemon,
                stdin=subprocess.DEVNULL, stdout=self.log, stderr=self.log,
                env=dict(PATH='/q4t-test-no-system-tools', LANG='C.UTF-8'))
        finally:
            os.close(lease)
            if bootstrap_child is not None:
                bootstrap_child.close()
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

    def detach(self):
        self.bootstrap.settimeout(5)
        ready = journal.receive(self.bootstrap)
        if (ready['guardian_pid'] != self.process.pid or
                ready['service_id'] != self.service_id or
                not ready['business_ready'] or ready['daemon_detached']):
            raise AssertionError('invalid daemon readiness handshake')
        journal.send(self.bootstrap, dict(version=1, service_id=self.service_id,
                                          op='detach'))
        result = journal.receive(self.bootstrap)
        if not result['daemon_detached'] or result['guardian_pid'] != self.process.pid:
            raise AssertionError('daemon commit not acknowledged')
        self.bootstrap.close()
        self.bootstrap = None
        return result

    def persistent(self):
        return json.loads((self.directory/store.STATE).read_text())['state']

    def assert_lease_free(self):
        fd = os.open(self.lease_path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)

    def close(self):
        if self.bootstrap is not None:
            self.bootstrap.close()
            self.bootstrap = None
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

    def test_qualified_capacity_binding_survives_independent_gateway_admission(self):
        service = self.launch(models=True, gateway=True, capacity_fingerprint='a'*64)
        current = service.wait(lambda s: s['business_ready'])
        self.assertEqual(current['model_capacity_sha256'], 'a'*64)
        reply = json.loads((service.directory/'capacity-binding.json').read_text())
        self.assertTrue(reply['ok'])
        self.assertEqual(current['model_generation'], 1)
        self.assertNotIn('binding', current)
        self.assertTrue(service.stop()['cleanup_confirmed'])
        service.assert_lease_free()

    def test_malformed_capacity_cannot_open_model_or_create_another_generation(self):
        service = self.launch(models=True, gateway=True, capacity_fingerprint='not-qualified')
        service.wait(lambda s: (service.directory/'capacity-binding.json').exists())
        reply = json.loads((service.directory/'capacity-binding.json').read_text())
        self.assertFalse(reply['ok'])
        current = service.status()
        self.assertFalse(current['business_ready'])
        self.assertFalse(current['recovery_frozen'])
        self.assertEqual(current['model_generation'], 1)
        self.assertIsNone(current['model_capacity_sha256'])
        self.assertTrue(service.stop()['cleanup_confirmed'])
        service.assert_lease_free()

    def test_gateway_failure_preserves_controller_and_model(self):
        service = self.launch(models=True, gateway=True)
        first = service.wait(lambda s: s['business_ready'])
        escaped = children(first['gateway_pid'])
        self.assertTrue(escaped)
        signal_identity(first['gateway_pid'], first['gateway_start_ticks'],
                        signal.SIGKILL)
        second = service.wait(lambda s: s['business_ready'] and
                              s['gateway_generation'] == 2)
        self.assertEqual(second['model_pid'], first['model_pid'])
        self.assertEqual(second['controller_pid'], first['controller_pid'])
        self.assertEqual(second['model_generation'], 1)
        self.assertEqual(len(service.persistent()['attempts']['gateway']), 1)
        for pid in escaped:
            self.assertFalse(Path(f'/proc/{pid}').exists())
        self.assertTrue(service.stop()['cleanup_confirmed'])
        service.assert_lease_free()

    def test_controller_failure_keeps_gateway_and_rebinds_new_model(self):
        service = self.launch(models=True, gateway=True)
        first = service.wait(lambda s: s['business_ready'])
        escaped = children(first['gateway_pid'])
        signal_identity(first['controller_pid'], first['controller_start_ticks'],
                        signal.SIGKILL)
        second = service.wait(lambda s: s['business_ready'] and
                              s['model_generation'] == 2)
        self.assertEqual(second['gateway_pid'], first['gateway_pid'])
        self.assertEqual(second['gateway_bound_model'], 2)
        self.assertNotEqual(second['model_pid'], first['model_pid'])
        self.assertEqual(children(second['gateway_pid']), escaped)
        self.assertTrue(service.stop()['cleanup_confirmed'])

    def test_live_gateway_credentials_are_not_public_status(self):
        service = self.launch(models=True, gateway=True)
        status = service.wait(lambda s: s['business_ready'])
        self.assertNotIn('binding', status)
        with self.assertRaises(ValueError):
            control.binding(service.service_id)
        self.assertTrue(service.status()['business_ready'])

    def test_cold_guardian_never_authorizes_nvml_free_probe(self):
        service = self.launch(models=True, probe_only=True)
        service.wait(lambda s: (service.directory/'probe-controller-1.json').exists())
        reply = json.loads((service.directory/'probe-controller-1.json').read_text())
        self.assertFalse(reply['ok'])
        self.assertEqual(reply['result']['reason'], 'probe_not_authorized')
        self.assertFalse(service.status()['continuous_model_recovery'])

    def test_probe_permission_is_once_and_survives_controller_replacement(self):
        service = self.launch(models=True, probe_lifecycle=True)
        first = service.wait(lambda s: s['business_ready'])
        signal_identity(first['model_pid'], first['model_start_ticks'], signal.SIGKILL)
        service.wait(lambda s: (service.directory/'probes.json').exists())
        replies = json.loads((service.directory/'probes.json').read_text())
        self.assertTrue(replies[0]['ok'] and replies[0]['result']['allowed'])
        self.assertFalse(replies[1]['ok'])
        self.assertEqual(service.status()['probe_used_for_generation'], 1)
        signal_identity(first['controller_pid'], first['controller_start_ticks'], signal.SIGKILL)
        service.wait(lambda s: (service.directory/'probe-controller-2.json').exists())
        repeated = json.loads((service.directory/'probe-controller-2.json').read_text())
        self.assertEqual(repeated['result']['reason'], 'probe_not_authorized')
        self.assertEqual(service.status()['model_generation'], 1)
        self.assertTrue(service.stop()['cleanup_confirmed'])

    def test_auxiliary_health_keeps_business_and_core_admission_survives_ledger_loss(self):
        service = self.launch(models=True, aux_health=True)
        first = service.wait(lambda s: s['state'] == 'READY_DEGRADED')
        self.assertTrue(first['business_ready'])
        self.assertEqual(first['controller_health']['reason'], 'query_timeout')
        signal_identity(first['journal_pid'], first['journal_start_ticks'], signal.SIGKILL)
        service.wait(lambda s: s['recovery_frozen'])
        (service.directory/'admission.closed').touch()
        closed = service.wait(lambda s: not s['business_ready'])
        self.assertTrue(closed['model_ready'])
        self.assertEqual(closed['model_pid'], first['model_pid'])
        (service.directory/'admission.closed').unlink()
        service.wait(lambda s: s['business_ready'])
        result = service.stop()
        self.assertFalse(result['stop_durable'])

    def test_malformed_optional_observation_keeps_model_and_controller(self):
        service = self.launch(models=True, aux_health=True)
        first = service.wait(lambda s: s['business_ready'])
        (service.directory/'auxiliary-invalid.json').write_text(json.dumps(
            dict(queued_bytes=999999, worker_state='not a control command')))
        degraded = service.wait(lambda s: s['auxiliary_projection_errors'] > 0 and
            s['controller_health']['reason'] == 'auxiliary_io_invalid')
        self.assertTrue(degraded['business_ready'])
        self.assertEqual(degraded['controller_health']['observation'], 'UNKNOWN')
        for key in ('model_pid', 'controller_pid', 'model_generation'):
            self.assertEqual(degraded[key], first[key])
        (service.directory/'auxiliary-invalid.json').unlink()
        service.wait(lambda s: s['controller_health']['reason'] == 'query_timeout')
        self.assertTrue(service.stop()['cleanup_confirmed'])

    def test_model_quota_denial_keeps_loop_and_gateway_alive(self):
        service = self.launch(models=True, gateway=True, quota=True)
        first = service.wait(lambda s: s['business_ready'])
        signal_identity(first['model_pid'], first['model_start_ticks'], signal.SIGKILL)
        second = service.wait(lambda s: s['model_generation'] == 2 and
            (service.directory/'quota-blocked.json').exists())
        self.assertFalse(second['business_ready'] or second['model_ready'])
        self.assertGreater(second['quota_wait_ms'], 40000)
        self.assertEqual(second['gateway_pid'], first['gateway_pid'])
        self.assertEqual(second['controller_pid'], first['controller_pid'])
        self.assertTrue(service.stop()['cleanup_confirmed'])

    def test_quota_restore_receipt_opens_only_the_current_bound_generation(self):
        service = self.launch(models=True, gateway=True, quota=True,
                              usage_handoff=True, quota_restore='valid')
        first = service.wait(lambda s: s['business_ready'])
        signal_identity(first['model_pid'], first['model_start_ticks'], signal.SIGKILL)
        second = service.wait(lambda s: s['business_ready'] and s['model_generation'] == 2)
        self.assertEqual(second['quota_wait_ms'], 0)
        self.assertEqual(second['controller_pid'], first['controller_pid'])
        self.assertTrue(service.stop()['cleanup_confirmed'])

    def test_invalid_quota_receipts_keep_conservative_gate_without_restarting(self):
        for mode in ('stale', 'boot', 'digest'):
            with self.subTest(mode=mode):
                service = self.launch(models=True, gateway=True, quota=True,
                                      usage_handoff=True, quota_restore=mode)
                first = service.wait(lambda s: s['business_ready'])
                signal_identity(first['model_pid'], first['model_start_ticks'], signal.SIGKILL)
                blocked = service.wait(lambda s: s['model_generation'] == 2 and
                    (service.directory/'quota-blocked.json').exists())
                self.assertFalse(blocked['business_ready'])
                self.assertGreater(blocked['quota_wait_ms'], 40000)
                self.assertEqual(blocked['controller_pid'], first['controller_pid'])
                self.assertTrue(service.stop()['cleanup_confirmed'])

    def test_planned_stop_allows_controller_handoff_before_signalling_model(self):
        service = self.launch(models=True, gateway=True, quota=True,
                              usage_handoff=True, stop_handoff=True)
        service.wait(lambda s: s['business_ready'])
        self.assertTrue(service.stop()['cleanup_confirmed'])
        receipt = json.loads((service.directory/'handoff-order.json').read_bytes())
        self.assertTrue(receipt['before_alive'] and receipt['after_alive'])

    def test_production_controller_cpu_backend_model_and_controller_recovery(self):
        service = self.launch(production=True, gateway=True)
        first = service.wait(lambda s: s['business_ready'], timeout=15)
        signal_identity(first['model_pid'], first['model_start_ticks'], signal.SIGKILL)
        second = service.wait(lambda s: s['business_ready'] and s['model_generation'] == 2, timeout=15)
        self.assertEqual(first['controller_pid'], second['controller_pid'])
        signal_identity(second['controller_pid'], second['controller_start_ticks'], signal.SIGKILL)
        third = service.wait(lambda s: s['business_ready'] and s['model_generation'] == 3, timeout=20)
        self.assertEqual(third['controller_generation'], 2)
        self.assertEqual(third['gateway_pid'], first['gateway_pid'])
        self.assertEqual(len(service.persistent()['attempts']['model']), 2)
        self.assertTrue(service.stop()['cleanup_confirmed'])

    def test_production_blocked_log_preserves_business_and_control_generation(self):
        service = self.launch(production=True, gateway=True)
        first = service.wait(lambda s: s['business_ready'], timeout=15)
        model_directory = service.directory/'controller-1/model-1'
        os.mkfifo(model_directory/'model.log', 0o600)
        (service.directory/'log.flood').touch()
        degraded = service.wait(lambda s: s['state'] == 'READY_DEGRADED' and
            s['controller_health']['auxiliary_io']['worker_state'] == 'DISABLED', timeout=12)
        self.assertTrue((service.directory/'log.drained').exists())
        self.assertTrue(degraded['business_ready'])
        for field in ('model_pid', 'model_generation', 'controller_pid', 'gateway_pid'):
            self.assertEqual(degraded[field], first[field])
        stats = degraded['controller_health']['auxiliary_io']
        self.assertGreater(stats['dropped_bytes'], 0)
        self.assertLessEqual(stats['queued_bytes'], 128*1024)
        self.assertEqual(degraded['controller_health']['reason'], 'auxiliary_io_unavailable')
        self.assertTrue(service.stop()['cleanup_confirmed'])
        service.assert_lease_free()

    def test_production_monitor_failure_keeps_model_then_uses_single_cpu_probe(self):
        service = self.launch(production=True, gateway=True)
        first = service.wait(lambda s: s['business_ready'], timeout=15)
        (service.directory/'monitor.unavailable').touch()
        degraded = service.wait(lambda s: s['state'] == 'READY_DEGRADED' and
            s['controller_health']['reason'] == 'nvml_timeout', timeout=12)
        self.assertEqual(degraded['model_pid'], first['model_pid'])
        signal_identity(first['model_pid'], first['model_start_ticks'], signal.SIGKILL)
        second = service.wait(lambda s: s['business_ready'] and s['model_generation'] == 2, timeout=15)
        self.assertEqual(second['probe_used_for_generation'], 1)
        self.assertEqual(second['controller_pid'], first['controller_pid'])
        self.assertTrue(service.stop()['cleanup_confirmed'])

    def test_simultaneous_gateway_controller_failure_cleans_both_domains(self):
        service = self.launch(models=True, gateway=True)
        first = service.wait(lambda s: s['business_ready'])
        escaped = children(first['gateway_pid'])
        for role in ('controller', 'gateway'):
            signal_identity(first[role+'_pid'], first[role+'_start_ticks'],
                            signal.SIGKILL)
        second = service.wait(lambda s: s['business_ready'] and
                              s['model_generation'] == 2, timeout=15)
        self.assertEqual(second['controller_generation'], 2)
        self.assertEqual(second['gateway_generation'], 2)
        for pid in escaped:
            self.assertFalse(Path(f'/proc/{pid}').exists())
        self.assertTrue(service.stop()['cleanup_confirmed'])

    def test_controller_crash_cleans_escaped_descendants_before_replacement(self):
        service = self.launch(escape=True)
        first = service.ready()
        escaped = set(children(first['controller_pid']))
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

    def test_model_exec_requires_durable_identity_and_readiness(self):
        service = self.launch(models=True)
        current = service.wait(lambda s: s['business_ready'])
        state = service.persistent()
        self.assertEqual(state['active']['model']['phase'], 'ready')
        self.assertEqual(state['active']['model']['owner']['pid'],
                         current['model_pid'])
        marker = json.loads((service.directory/'model-1.started').read_text())
        self.assertEqual(marker['pid'], current['model_pid'])
        self.assertEqual(service.stop()['state'], 'STOPPED')
        self.assertTrue(all(value is None for value in
                            service.persistent()['active'].values()))
        service.assert_lease_free()

    def test_model_crash_recovers_with_same_controller_and_budget(self):
        service = self.launch(models=True)
        first = service.wait(lambda s: s['business_ready'])
        signal_identity(first['model_pid'], first['model_start_ticks'], signal.SIGKILL)
        second = service.wait(lambda s: s['business_ready'] and
                               s['model_generation'] == 2)
        self.assertEqual(second['controller_pid'], first['controller_pid'])
        self.assertNotEqual(second['model_pid'], first['model_pid'])
        self.assertFalse(Path(f"/proc/{first['model_pid']}").exists())
        self.assertEqual(len(service.persistent()['attempts']['model']), 1)
        self.assertEqual(service.stop()['state'], 'STOPPED')

    def test_controller_crash_retires_model_and_escaped_child_before_new_grant(self):
        service = self.launch(models=True, escape_model=True)
        first = service.wait(lambda s: s['business_ready'])
        escaped = children(first['model_pid'])
        self.assertTrue(escaped)
        signal_identity(first['controller_pid'], first['controller_start_ticks'],
                        signal.SIGKILL)
        second = service.wait(lambda s: s['business_ready'] and
                               s['controller_generation'] == 2)
        self.assertEqual(second['model_generation'], 2)
        for pid in [first['model_pid'], first['controller_pid'], *escaped]:
            self.assertFalse(Path(f'/proc/{pid}').exists())
        self.assertEqual(len(service.persistent()['attempts']['model']), 1)
        self.assertEqual(service.stop()['state'], 'STOPPED')

    def test_stop_after_authorization_does_not_execute_model(self):
        service = self.launch(models=True, pause_at='authorized')
        current = service.wait(lambda s: s['model_generation'] == 1)
        self.assertIsNone(current['model_pid'])
        self.assertFalse(current['business_ready'])
        self.assertEqual(service.stop()['state'], 'STOPPED')
        self.assertFalse((service.directory/'model-1.started').exists())
        self.assertIsNone(service.persistent()['active']['model'])

    def test_stop_after_registration_cleans_unexecuted_model(self):
        service = self.launch(models=True, pause_at='spawned')
        current = service.wait(lambda s: s['model_pid'] is not None)
        self.assertFalse(current['business_ready'])
        self.assertEqual(service.stop()['state'], 'STOPPED')
        self.assertFalse(Path(f"/proc/{current['model_pid']}").exists())
        self.assertFalse((service.directory/'model-1.started').exists())
        self.assertIsNone(service.persistent()['active']['model'])

    def test_unknown_device_allows_controller_but_denies_model(self):
        service = self.launch(models=True, device_unknown=True)
        first = service.ready()
        current = service.wait(lambda s: s['controller_events'] >=
                               first['controller_events']+3)
        self.assertTrue(current['controller_ready'])
        self.assertEqual(current['model_generation'], 0)
        self.assertFalse(list(service.directory.glob('model-*.started')))
        self.assertEqual(service.stop()['state'], 'STOPPED')

    def test_journal_failure_keeps_model_ready_but_cannot_restart_it(self):
        service = self.launch(models=True)
        first = service.wait(lambda s: s['business_ready'])
        signal_identity(first['journal_pid'], first['journal_start_ticks'], signal.SIGKILL)
        current = service.wait(lambda s: s['recovery_frozen'])
        self.assertTrue(current['business_ready'])
        self.assertEqual(current['state'], 'READY_DEGRADED')
        self.assertEqual(current['model_pid'], first['model_pid'])
        signal_identity(first['model_pid'], first['model_start_ticks'], signal.SIGKILL)
        current = service.wait(lambda s: not s['business_ready'] and
                               s['controller_events'] >= first['controller_events']+5)
        self.assertEqual(current['model_generation'], 1)
        self.assertEqual(current['controller_pid'], first['controller_pid'])
        self.assertFalse(service.stop()['stop_durable'])

    def test_repeated_model_fault_quarantines_across_generations(self):
        service = self.launch(models=True)
        first = service.wait(lambda s: s['business_ready'])
        signal_identity(first['model_pid'], first['model_start_ticks'], signal.SIGKILL)
        second = service.wait(lambda s: s['business_ready'] and
                              s['model_generation'] == 2)
        signal_identity(second['model_pid'], second['model_start_ticks'], signal.SIGKILL)
        current = service.wait(lambda s: s['state'] == 'QUARANTINED' and
                               s['model_pid'] is None)
        self.assertFalse(current['business_ready'])
        state = service.persistent()
        self.assertEqual(state['quarantine'], 'repeated_model_fault')
        self.assertEqual(state['generations']['model'], 2)
        self.assertEqual(service.stop()['state'], 'STOPPED')

    def test_corrupt_ledger_keeps_live_control_without_start_or_false_stop(self):
        service = self.launch(bad_ledger=True)
        current = service.wait(lambda s: s['recovery_frozen'])
        self.assertIsNone(current['controller_pid'])
        result = service.stop()
        self.assertFalse(result['stop_durable'] or result['cleanup_confirmed'])
        self.assertEqual(children(service.process.pid), [])

    def test_unreconciled_old_grant_cannot_be_reset_by_new_guardian(self):
        service = self.launch(stale_grant=True)
        current = service.wait(lambda s: s['recovery_frozen'])
        self.assertIsNone(current['controller_pid'])
        self.assertEqual(service.persistent()['generations']['controller'], 1)
        result = service.stop()
        self.assertTrue(result['stop_durable'])
        self.assertFalse(result['cleanup_confirmed'])
        self.assertIsNotNone(service.persistent()['active']['controller'])

    def test_daemon_requires_business_readiness_and_explicit_detach(self):
        service = self.launch(models=True, daemon=True)
        result = service.detach()
        self.assertTrue(result['business_ready'])
        self.assertTrue(service.status()['daemon_detached'])
        self.assertIsNone(service.process.poll())
        self.assertEqual(service.stop()['state'], 'STOPPED')
        service.assert_lease_free()

    def test_abandoned_daemon_start_stops_and_does_not_execute_model(self):
        service = self.launch(models=True, daemon=True, pause_at='authorized')
        service.wait(lambda s: s['model_generation'] == 1)
        service.bootstrap.close(); service.bootstrap = None
        self.assertEqual(service.process.wait(timeout=5), 0)
        self.assertEqual(service.persistent()['desired'], 'STOPPED')
        self.assertFalse(list(service.directory.glob('model-*.started')))
        service.assert_lease_free()

    def test_daemon_readiness_timeout_is_a_durable_clean_stop(self):
        service = self.launch(models=True, daemon=True, pause_at='authorized',
                              startup_ms=1000)
        self.assertEqual(service.process.wait(timeout=5), 0)
        self.assertEqual(service.persistent()['desired'], 'STOPPED')
        self.assertTrue(all(value is None for value in
                            service.persistent()['active'].values()))
        service.assert_lease_free()

    def launcher_fixture(self, mode):
        output = Path(tempfile.mkdtemp(prefix='launcher-', dir=self.output))
        with (output/'launcher.log').open('wb') as log:
            helper = subprocess.run([sys.executable, '-I', '-B', '-W', 'error',
                str(Path(__file__).resolve()), '--guardian', str(BINARY),
                '--launcher-fixture', str(output), '--launch-mode', mode],
                stdout=log, stderr=log, timeout=10)
        self.assertEqual(helper.returncode, 0, (output/'launcher.log').read_text())
        return json.loads((output/'result.json').read_text())

    def reap_guardian(self, pid):
        end = time.monotonic()+5
        while time.monotonic() < end:
            waited, status = os.waitpid(pid, os.WNOHANG)
            if waited:
                self.assertEqual(os.waitstatus_to_exitcode(status), 0)
                return
            time.sleep(0.02)
        self.fail('adopted Guardian did not complete cleanup')

    def test_committed_daemon_survives_short_lived_launcher(self):
        result = self.launcher_fixture('daemon')
        current = control.request(result['service_id'], 'status')
        self.assertTrue(current['business_ready'] and current['daemon_detached'])
        stopped = control.request(result['service_id'], 'stop', timeout=6)
        self.assertTrue(stopped['cleanup_confirmed'])
        self.reap_guardian(result['guardian_pid'])

    def test_foreground_parent_death_is_a_durable_stop(self):
        result = self.launcher_fixture('foreground')
        self.reap_guardian(result['guardian_pid'])
        state = json.loads((Path(result['directory'])/store.STATE).read_text())['state']
        self.assertEqual(state['desired'], 'STOPPED')
        self.assertTrue(all(value is None for value in state['active'].values()))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--guardian', type=Path, default=BINARY)
    parser.add_argument('--launcher-fixture', type=Path)
    parser.add_argument('--launch-mode', choices=('foreground', 'daemon'))
    args, rest = parser.parse_known_args()
    BINARY = args.guardian.resolve(strict=True)
    if args.launcher_fixture:
        service = Service(args.launcher_fixture, models=True,
                          daemon=args.launch_mode == 'daemon')
        if args.launch_mode == 'daemon':
            service.detach()
        else:
            service.wait(lambda s: s['business_ready'])
        (args.launcher_fixture/'result.json').write_text(json.dumps(dict(
            guardian_pid=service.process.pid, service_id=service.service_id,
            directory=str(service.directory))))
        # Deliberately exit the short-lived CPU launcher. The test runner's
        # subreaper adopts Guardian and verifies the selected lifecycle.
        os._exit(0)
    unittest.main(argv=[sys.argv[0], *rest])

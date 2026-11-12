"""Targeted CPU observation tests; real local IPC/HTTP, no model or CUDA start."""
from copy import deepcopy
import json
import os
from pathlib import Path
import signal
import socket
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock

from tools.serve_host import native, native_exporter as exporter
from tools.serve_recovery import bundle, control, journal, ledger

ROOT = Path(__file__).resolve().parents[2]


class NativeTests(unittest.TestCase):
    def setUp(self):
        parent = ROOT / '.q4t-work/serve-native-observation-20261109/cpu-fixtures'
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.real_state = ROOT / 'build/service-native-state'
        for name in ('service.json', 'ledger.json'):
            path = self.directory / name
            path.write_bytes((self.real_state / name).read_bytes())
            path.chmod(0o600)
        self.desc = json.loads((self.directory / 'service.json').read_text())
        self.package = Path(self.desc['package'])
        self.digest = self.desc['manifest_sha256']
        self.status = json.loads((ROOT / 'docs/evidence/serve-progress-business-20261109/'
            'business-02/execution/ready-2.json').read_text())

    def binding(self):
        return native.bind(self.package, self.digest, self.directory)

    def wait(self, predicate, seconds=8):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            if predicate():
                return
            time.sleep(.025)
        self.fail('bounded CPU condition did not become true')

    def test_birth_namespace_and_descriptor_replacement_are_not_trusted(self):
        pid = os.getpid()
        start = journal.process_start(pid)
        context = {n: native.inode('/proc/self/ns/' + n) for n in ('pid', 'user', 'net', 'mnt')}
        executable = native.inode('/proc/self/exe')
        native.process(pid, start, executable, context)
        with self.assertRaises(ValueError):
            native.process(pid, start + 1, executable, context)
        changed = dict(context, pid=[0, 0])
        with self.assertRaises(ValueError):
            native.process(pid, start, executable, changed)
        binding = self.binding()
        self.desc['manifest_sha256'] = '0' * 64
        (self.directory / 'service.json').write_text(json.dumps(self.desc))
        query = mock.Mock(side_effect=AssertionError('changed binding cannot contact service'))
        result = native.collect(binding, request=query)
        self.assertEqual(result['reason'], 'identity_changed')
        query.assert_not_called()

    def test_auxiliary_unknown_and_missing_resources_do_not_override_ready(self):
        binding = self.binding()
        value = deepcopy(self.status)
        value['service_id'] = self.desc['service_id']
        value['state'] = 'READY_DEGRADED'
        value['controller_health']['observation'] = 'UNKNOWN'
        broken = mock.Mock(side_effect=OSError('unavailable'))
        result = native.collect(binding, request=lambda *_a, **_k: value,
            check_process=lambda *_a, **_k: None, resources=broken, runtime=broken)
        self.assertTrue(result['business_ready'])
        self.assertEqual(result['observation'], 'UNKNOWN')
        self.assertFalse(result['sources']['runtime'])
        self.assertFalse(result['sources']['process'])
        text = native.metrics(result)
        self.assertIn('q4t_service_business_ready 1', text)
        self.assertNotIn('q4t_service_process_rss_bytes', text)
        self.assertNotIn('worker_pid', text)
        device = deepcopy(value)
        device['controller_health']['observation'] = 'DEVICE_FAILURE'
        self.assertEqual(native.project_status(device)['observation'], 'DEVICE_FAILURE')
        missing = deepcopy(value); missing['model_pid'] = None
        with self.assertRaises(ValueError):
            native.project_status(missing)
        malformed = deepcopy(value)
        malformed['model_admission_open'] = False
        with self.assertRaises(ValueError):
            native.project_status(malformed)
        later = deepcopy(value);later['model_generation'] += 1
        responses = iter((value, later))
        result = native.collect(binding, request=lambda *_a, **_k: next(responses),
            check_process=lambda *_a, **_k: None, resources=broken, runtime=broken)
        self.assertEqual(result['reason'], 'identity_changed')
        self.assertIsNone(result['business_ready'])

    def test_actual_private_status_channel_has_no_mutating_operation(self):
        service_id = str(uuid.uuid4())
        self.desc['service_id'] = service_id
        (self.directory / 'service.json').write_text(json.dumps(self.desc))
        state = ledger.new_state(service_id, ledger.Stamp(native.stamp()['boot_id'], time.monotonic_ns()))
        (self.directory / 'ledger.json').write_text(json.dumps(dict(
            previous_sha256=None, sha256=ledger.digest(state), state=state)))
        binding = self.binding()
        binding['guardian'] = native.inode('/proc/self/exe')
        value = deepcopy(self.status)
        value.update(service_id=service_id, state='STARTING', guardian_pid=os.getpid(),
            guardian_start_ticks=journal.process_start(os.getpid()), controller_pid=None,
            model_pid=None, gateway_pid=None, business_ready=False)
        observed = []
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as listener:
            listener.bind(control.address(service_id));listener.listen(1);listener.settimeout(2)
            def reply():
                conn, _ = listener.accept()
                with conn:
                    observed.append(journal.receive(conn))
                    journal.send(conn, value)
            thread = threading.Thread(target=reply);thread.start()
            result = native.collect(binding)
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual([x['op'] for x in observed], ['status'])
        self.assertEqual(result['state'], 'STARTING')
        self.assertTrue(result['sources']['guardian'])

    def test_freshness_does_not_renew_ready_or_turn_missing_values_into_zero(self):
        now = native.stamp()
        value = native.empty(now=now)
        value.update(native.project_status(self.status), reason='none')
        value['sources']['guardian'] = True
        self.assertIs(native.current(value, now=dict(now, boottime=now['boottime'] + 2)), value)
        stale = native.current(value, now=dict(now, boottime=now['boottime'] + 4))
        self.assertEqual(stale['state'], 'UNKNOWN')
        self.assertNotIn('q4t_service_business_ready ', native.metrics(stale))
        changed = native.current(value, now=dict(now, boot_id=str(uuid.uuid4())))
        self.assertEqual(changed['reason'], 'source_stale')
        self.assertEqual(value['sampled'], now)

    def test_actual_http_bounds_methods_and_slow_connection(self):
        value = native.empty()
        for request, expected in ((b'POST /metrics HTTP/1.1\r\nHost: x\r\n\r\n', 405),
                (b'GET /metrics?x=1 HTTP/1.1\r\nHost: x\r\n\r\n', 400),
                (b'GET /metrics HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n', 400),
                (b'GET /metrics HTTP/1.1\r\nHost: x\r\nHost: y\r\n\r\n', 400),
                (b'x' * 8193, 431), (b'GET /stop HTTP/1.1\r\nHost: x\r\n\r\n', 404)):
            self.assertTrue(exporter.response(request, value).startswith(f'HTTP/1.1 {expected} '.encode()))
        head = exporter.response(b'HEAD /metrics HTTP/1.1\r\nHost: x\r\n\r\n', value)
        self.assertTrue(head.endswith(b'\r\n\r\n'))
        self.assertIn(b'Content-Length:', head)
        stop = threading.Event()
        collector = mock.Mock(value=value, reason='collector_unavailable')
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0));listener.listen(16)
            port = listener.getsockname()[1]
            thread = threading.Thread(target=exporter.serve, args=(listener, collector, stop))
            thread.start()
            try:
                with socket.create_connection(('127.0.0.1', port)) as slow:
                    slow.sendall(b'GET ')
                    with socket.create_connection(('127.0.0.1', port)) as client:
                        client.settimeout(1)
                        client.sendall(b'GET /status HTTP/1.1\r\nHost: x\r\n\r\n')
                        raw = bytearray()
                        while part := client.recv(65536):raw.extend(part)
                        self.assertEqual(json.loads(raw.partition(b'\r\n\r\n')[2])['state'], 'UNKNOWN')
                    slow.settimeout(3)
                    self.assertEqual(slow.recv(1), b'')
            finally:
                stop.set();thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_real_collector_timeout_reap_budget_and_single_owner(self):
        before = bundle.engine.sha(self.real_state / 'ledger.json')
        collector = exporter.Collector(self.package, self.digest, self.directory)
        self.addCleanup(collector.close)
        self.wait(lambda: collector.value is not None)
        self.assertEqual(collector.value['state'], 'STOPPED')
        competing = exporter.Collector(self.package, self.digest, self.directory)
        self.addCleanup(competing.close)
        self.wait(lambda: not competing.thread.is_alive())
        self.assertEqual(competing.started, 0)
        child = collector.child
        birth = journal.process_start(child.pid)
        fd = os.pidfd_open(child.pid)
        try:
            self.assertEqual(journal.process_start(child.pid), birth)
            signal.pidfd_send_signal(fd, signal.SIGSTOP)
        finally:
            os.close(fd)
        self.wait(lambda: collector.reaped >= 1 and collector.started == 2 and collector.value is not None)
        self.assertNotEqual(child.pid, collector.child.pid)
        collector.child.kill()
        self.wait(lambda: collector.started == 3 and collector.value is not None)
        collector.child.kill()
        self.wait(lambda: collector.reason == 'collector_budget')
        self.assertEqual(collector.started, 3)
        self.assertEqual(collector.reaped, 3)
        self.assertEqual(bundle.engine.sha(self.real_state / 'ledger.json'), before)
        self.assertTrue(collector.close())

    def test_current_stopped_delivery_readonly_and_legacy_v5_verification(self):
        before = {n: bundle.engine.sha(self.real_state / n) for n in ('service.json', 'ledger.json')}
        manifest = bundle.verify(self.package, self.digest)
        self.assertEqual(manifest['version'], 5)
        result = native.collect(self.binding())
        self.assertEqual(result['state'], 'STOPPED')
        self.assertFalse(result['business_ready'])
        self.assertTrue(result['sources']['ledger'])
        self.assertFalse(result['sources']['guardian'])
        public = json.dumps(result)
        for forbidden in ('guardian_pid', 'model_pid', str(self.directory), 'api_key', self.desc['service_id']):
            self.assertNotIn(forbidden, public)
        self.assertEqual(before, {n: bundle.engine.sha(self.real_state / n) for n in before})


if __name__ == '__main__':
    unittest.main()

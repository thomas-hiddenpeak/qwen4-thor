"""CPU admission contracts: failure isolation, binding, policy and input bounds."""
import copy
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import binding, gateway, gateway_config, journal
from tools.serve_gateway import gateway as frontend
from tools.serve_service import supervisor


class RouteInventoryTests(unittest.TestCase):
    def test_all_current_public_endpoints_are_declared(self):
        self.assertEqual(frontend.validate_routes(),
                         frontend.sha(frontend.API_CONTRACT))

    def test_missing_or_duplicate_application_operation_is_rejected(self):
        original = frontend.APPLICATION_ROUTES
        for changed in (original[1:], original+[original[0]]):
            with self.subTest(duplicate=len(changed) > len(original)):
                with mock.patch.object(frontend, 'APPLICATION_ROUTES', changed):
                    with self.assertRaisesRegex(ValueError, 'route inventory'):
                        frontend.validate_routes()

    def test_new_public_operation_cannot_silently_miss_frontend(self):
        output = Path(os.environ.get('Q4T_TEST_OUTPUT_DIR', ROOT/'build/route-tests'))
        output.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=output) as directory:
            contract = json.loads(frontend.API_CONTRACT.read_text())
            contract['paths']['/v1/new_public_api'] = {'post': {
                'operationId': 'cpu_missing_frontend'}}
            path = Path(directory)/'contract.json'
            path.write_text(json.dumps(contract))
            with mock.patch.object(frontend, 'API_CONTRACT', path):
                with self.assertRaisesRegex(ValueError, 'route inventory'):
                    frontend.validate_routes()


def config(directory, ports=(18181, 18182, 18183), nginx='/usr/sbin/nginx'):
    return dict(version=1, directory=str(directory), nginx=nginx,
        nginx_sha256='a'*64, binary_sha256='b'*64, protocol_sha256='c'*64,
        tls=None, policy=dict(version=2, listen_host='127.0.0.1',
            listen_port=ports[0], server_name='127.0.0.1',
            upstream_port=ports[1], upstream_operations_port=ports[2],
            allowed_client_cidrs=['127.0.0.1/32'],
            clients=[dict(id='alpha', api_key='A'*40), dict(id='beta', api_key='B'*40)]),
        backend_policy=dict(version=2, gateway_token='d'*64, fair_queue=True,
            clients=[dict(id=name, max_inflight=2, max_control_inflight=4,
                max_prompt_tokens=4096, max_output_tokens=256, max_body_bytes=1048576,
                requests_per_minute=30, tokens_per_minute=6000) for name in ('alpha', 'beta')]))


def snapshot(value, generation=1, token='e'*64, owner=None):
    raw = binding.policy_bytes(value['backend_policy'], token)
    return dict(model_owner=owner or supervisor.process_identity(os.getpid()),
        model_generation=generation, model_ready=True, business_ready=True,
        stop_requested=False, quota_wait_ms=0, state='READY',
        gateway_bound_model=generation,
        binding=dict(port=value['policy']['upstream_port'],
            operations_port=value['policy']['upstream_operations_port'],
            gateway_token=token, client_policy_sha256=hashlib.sha256(raw).hexdigest(),
            binary_sha256=value['binary_sha256'], protocol_sha256=value['protocol_sha256']))


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.config = config(ROOT/'build/recovery-gateway')

    def validate(self, value):
        return binding.validate(value, base=self.config['backend_policy'],
            port=18182, operations_port=18183, binary='b'*64, protocol='c'*64)

    def test_rotating_credentials_preserves_all_quota_and_fairness_fields(self):
        before = copy.deepcopy(self.config['backend_policy'])
        self.validate(snapshot(self.config)['binding'])
        self.assertEqual(self.config['backend_policy'], before)
        first = binding.policy_bytes(before, 'e'*64)
        second = binding.policy_bytes(before, 'f'*64)
        self.assertNotEqual(first, second)
        self.assertTrue(binding.quota_enforced(before))
        for client in before['clients']:
            client['requests_per_minute'] = client['tokens_per_minute'] = 0
        self.assertFalse(binding.quota_enforced(before))

    def test_changed_release_ports_policy_and_types_are_rejected(self):
        good = snapshot(self.config)['binding']
        for field, value in [('binary_sha256', 'd'*64), ('protocol_sha256', 'd'*64),
                ('port', True), ('port', 18184), ('operations_port', 18182),
                ('gateway_token', 'A'*64), ('client_policy_sha256', 'f'*64),
                ('extra', 'ignored')]:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.validate(dict(good, **{field: value}))

    def test_plaintext_lan_and_implicit_source_trust_are_rejected(self):
        gateway_config.validate(self.config)
        changed = copy.deepcopy(self.config)
        changed['policy']['listen_host'] = '192.168.2.2'
        with self.assertRaisesRegex(ValueError, 'plaintext'):
            gateway_config.validate(changed)
        changed['tls'] = dict(certificate='/private/cert.pem', key='/private/key.pem')
        gateway_config.validate(changed)
        changed['policy'].pop('allowed_client_cidrs')
        with self.assertRaises(ValueError):
            gateway_config.validate(changed)


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.config = config(ROOT/'build/recovery-gateway')
        self.snapshot = snapshot(self.config)
        self.channel = mock.Mock(service_id=str(uuid.uuid4()))
        self.admission = gateway.Admission(self.config, self.channel)
        self.patch = mock.patch.object(gateway.control, 'binding',
                                       side_effect=lambda _: self.snapshot)
        self.patch.start()
        self.admission.cached = self.admission.key(self.snapshot)
        self.admission.pidfd = os.pidfd_open(os.getpid())

    def tearDown(self):
        self.patch.stop()
        self.admission.close()

    def test_each_request_requires_fresh_root_admission(self):
        self.assertEqual(self.admission.request('data'), dict(port=18182, token='e'*64))
        self.assertEqual(self.admission.request('control')['port'], 18183)
        self.snapshot['business_ready'] = False
        self.assertEqual(self.admission.request('data'), dict(error='service_recovering'))
        self.snapshot['quota_wait_ms'] = 50
        self.assertEqual(self.admission.request('data'), dict(error='service_quota_wait'))

    def test_stale_generation_and_credential_never_forward(self):
        for change in (snapshot(self.config, generation=2),
                       snapshot(self.config, token='f'*64)):
            self.snapshot = change
            self.assertEqual(self.admission.request('data'), dict(error='service_recovering'))
        self.assertEqual(self.channel.begin.call_count, 0)

    def test_root_unavailable_does_not_reuse_cached_success_or_restart_model(self):
        with mock.patch.object(gateway.control, 'binding', side_effect=TimeoutError):
            self.assertEqual(self.admission.request('data'),
                             dict(error='service_binding_unavailable'))
        self.assertEqual(self.channel.begin.call_count, 0)
        self.assertEqual(self.admission.request('data')['token'], 'e'*64)

    def test_new_generation_verifies_policy_and_elf_without_metrics(self):
        self.admission.close()
        with mock.patch.object(supervisor, 'probe', return_value={}) as probe, \
             mock.patch.object(supervisor, 'classify', return_value='READY'):
            self.admission.update()
        self.assertFalse(probe.call_args.kwargs['include_metrics'])
        self.channel.bound.assert_called_once_with(1)
        self.assertEqual(self.admission.request('data')['port'], 18182)

    def test_failed_verification_closes_binding_without_recovery_action(self):
        self.admission.close()
        with mock.patch.object(supervisor, 'probe', side_effect=ValueError):
            self.admission.update()
        self.assertIsNone(self.admission.cached)
        self.channel.bound.assert_not_called()
        self.channel.unbound.assert_called()
        self.channel.begin.assert_not_called()

    def test_generation_changes_during_verification_are_rejected(self):
        self.admission.close()
        def change(*_args, **_kwargs):
            self.snapshot = snapshot(self.config, generation=2)
            return {}
        with mock.patch.object(supervisor, 'probe', side_effect=change), \
             mock.patch.object(supervisor, 'classify', return_value='READY'):
            self.admission.update()
        self.assertIsNone(self.admission.cached)
        self.channel.bound.assert_not_called()

    def test_true_quarantine_and_stop_have_distinct_response_codes(self):
        self.snapshot['business_ready'] = False
        self.snapshot['state'] = 'QUARANTINED'
        self.assertEqual(self.admission.request('data')['error'], 'service_quarantined')
        self.snapshot['stop_requested'] = True
        self.assertEqual(self.admission.request('data')['error'], 'service_stopping')


class AdmissionServerTests(unittest.TestCase):
    def setUp(self):
        self.decide = mock.Mock(return_value=dict(port=18182, token='e'*64))
        self.server = gateway.AdmissionServer('a'*64, ['alpha'], self.decide)
        self.header = (b'GET /authorize HTTP/1.1\r\nX-Q4T-Admission-Secret: '+
            b'a'*64+b'\r\nX-Q4T-Client: alpha\r\nX-Q4T-Lane: data\r\n\r\n')

    def tearDown(self):
        self.server.close()

    def test_private_admission_uses_fixed_small_response(self):
        response = self.server.response(self.header)
        self.assertIn(b'204 No Content', response)
        self.assertIn(b'X-Q4T-Token: '+b'e'*64, response)
        self.decide.assert_called_once_with('data')

    def test_bad_secret_header_duplicates_method_or_body_never_forward(self):
        for raw in (self.header.replace(b'a'*64, b'b'*64),
                    self.header.replace(b'GET ', b'POST '), self.header+b'body',
                    self.header.replace(b'alpha', b'beta'),
                    self.header.replace(b'\r\n\r\n', b'\r\nX-Q4T-Lane: control\r\n\r\n'),
                    self.header.replace(b'\r\n\r\n', b'\r\nTransfer-Encoding: chunked\r\n\r\n')):
            with self.subTest(raw=raw[:30]):
                self.assertIn(b'403 Forbidden', self.server.response(raw))
        self.decide.assert_not_called()

    def test_admission_failure_or_injection_is_a_fixed_503(self):
        for result in (dict(error='service_recovering'), dict(error='x\r\nInjected: yes'),
                       dict(port=18182, token='e'*64+'\r\n')):
            self.decide.return_value = result
            response = self.server.response(self.header)
            self.assertIn(b'503 Service Unavailable', response)
            self.assertNotIn(b'Injected', response)

    def test_socket_capacity_and_deadline_are_bounded(self):
        connections = []
        try:
            for _ in range(9):
                connections.append(socket.create_connection(('127.0.0.1', self.server.port)))
                self.server.step(0)
            self.assertEqual(len(self.server.connections), 8)
            for item in self.server.connections.values():
                item[0] = 0
            self.server.step(0)
            self.assertEqual(len(self.server.connections), 0)
        finally:
            for sock in connections:
                sock.close()


if __name__ == '__main__':
    unittest.main()

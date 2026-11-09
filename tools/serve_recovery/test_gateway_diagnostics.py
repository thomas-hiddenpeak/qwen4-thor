"""Typed rejection, actual Guardian IPC, and fresh observation projection."""
import copy
import json
import os
from pathlib import Path
import socket
import unittest
import uuid
from unittest import mock

from tools.serve_host import native
from tools.serve_recovery import binding, control, gateway, journal, test_gateway
from tools.serve_recovery import test_gateway_capacity, test_runtime
from tools.serve_service import supervisor


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.config = test_gateway.config(Path.cwd()/'build/diagnostic-fixture')
        self.snapshot = test_gateway.snapshot(self.config)
        self.channel = mock.Mock(service_id=str(uuid.uuid4()))
        self.admission = gateway.Admission(self.config, self.channel)
        self.addCleanup(self.admission.close)
        self.patch = mock.patch.object(gateway.control, 'binding',
                                       side_effect=lambda _: self.snapshot)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def assert_reason(self, reason):
        self.assertEqual(self.channel.unbound.call_args.kwargs['reason'], reason)
        self.channel.begin.assert_not_called()

    def test_observation_transport_and_malformed_input_are_distinct_and_redacted(self):
        for failure, reason in ((OSError('private-token'), 'gateway_observation_unavailable'),
                                (ValueError('private-token'), 'gateway_observation_invalid'),
                                (TypeError('private-token'), 'gateway_observation_invalid')):
            self.admission.retry_at = 0
            with mock.patch.object(supervisor, 'probe', side_effect=failure):
                self.admission.update()
            self.assert_reason(reason)
            self.assertIsNone(self.admission.pidfd)
            self.assertNotIn('private-token', str(self.channel.mock_calls))

    def test_model_not_ready_stop_and_missing_binding_do_not_probe(self):
        for change, reason in ((dict(model_ready=False), 'gateway_model_not_ready'),
                (dict(stop_requested=True), 'gateway_stopping'),
                (dict(binding=None), 'gateway_binding_unavailable')):
            self.snapshot = dict(test_gateway.snapshot(self.config), **change)
            with mock.patch.object(supervisor, 'probe') as probe:
                self.admission.update()
                probe.assert_not_called()
            self.assert_reason(reason)

    def test_control_owner_and_changed_generation_are_distinct(self):
        with mock.patch.object(gateway.control, 'binding', side_effect=OSError('hidden')):
            self.admission.update()
        self.assert_reason('gateway_control_unavailable')
        wrong = dict(self.snapshot['model_owner'], start_ticks=1)
        with mock.patch.object(supervisor, 'process_identity', return_value=wrong):
            self.admission.update()
        self.assert_reason('gateway_identity_mismatch')
        self.admission.retry_at = 0
        later = test_gateway.snapshot(self.config, generation=2)
        with mock.patch.object(self.admission, 'fresh', side_effect=[self.snapshot, later]), \
             mock.patch.object(supervisor, 'probe', return_value={}), \
             mock.patch.object(supervisor, 'classify', return_value='READY'):
            self.admission.update()
        self.assert_reason('gateway_generation_changed')
        self.assertIsNone(self.admission.pidfd)

    def test_client_policy_wait_recovers_without_replacing_model_or_binding(self):
        with mock.patch.object(supervisor, 'probe', return_value={}), \
             mock.patch.object(supervisor, 'classify', return_value='READY'), \
             mock.patch.object(self.admission, 'update_policy', return_value=False):
            self.admission.update()
        self.assert_reason('gateway_client_policy_unavailable')
        original = self.admission.pidfd
        self.snapshot['gateway_bound_model'] = 0
        with mock.patch.object(supervisor, 'probe') as probe, \
             mock.patch.object(self.admission, 'update_policy', return_value=True):
            self.admission.update()
            probe.assert_not_called()
        self.assertEqual(self.admission.pidfd, original)
        self.channel.bound.assert_called_once_with(1)
        self.channel.begin.assert_not_called()

    def test_channel_emits_closed_reason_and_exact_generation_over_real_socket(self):
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(left.close);self.addCleanup(right.close)
        channel = control.GatewayChannel.__new__(control.GatewayChannel)
        channel.sock, channel.service_id = left, str(uuid.uuid4())
        channel.generation, channel.sequence = 2, 0
        channel.unbound('gateway_capacity_mismatch', 3)
        message = journal.receive(right)
        self.assertEqual(message['binding_failure'], dict(
            reason='gateway_capacity_mismatch', model_generation=3))
        for reason, generation in [('secret', 3), ('none', 3),
                                   ('gateway_capacity_mismatch', True),
                                   ('gateway_capacity_mismatch', -1)]:
            with self.assertRaises(ValueError):channel.unbound(reason, generation)
        channel.unbound()
        self.assertNotIn('binding_failure', journal.receive(right))


class ClassifierTests(unittest.TestCase):
    def test_actual_classifier_names_capacity_failure_without_relaxing_contract(self):
        fixture = test_gateway_capacity.GatewayCapacityTests()
        fixture.setUp()
        for change in (dict(max_context_tokens=8192), dict(mtp_k=True),
                       dict(default_output_tokens=256), dict(request_deadline_ms=1200000)):
            accepted, channel = fixture.verify(fixture.config, change)
            self.assertFalse(accepted)
            self.assertEqual(channel.unbound.call_args.kwargs['reason'],
                             'gateway_capacity_mismatch')
            channel.bound.assert_not_called()

    def test_actual_classifier_distinguishes_identity_policy_and_health(self):
        cfg = test_gateway.config(Path.cwd()/'build/diagnostic-classifier')
        value = test_gateway.snapshot(cfg)
        expected = binding.validate(value['binding'], base=cfg['backend_policy'],
            port=18182, operations_port=18183, binary='b'*64, protocol='c'*64)
        process = dict(executable_sha256='b'*64, listener_scope='loopback')
        observation = dict(process=process, operations_process=process, protocol='c'*64,
            configuration=dict(supervisor.expected_configuration(), operations_port=18183),
            client_policy=dict(expected, profile='q4t-gateway-clients-v1',
                               credential_values_redacted=True, mutable=False),
            operations_port=18183, observability_version=5, ready=True,
            health_status='healthy', metrics=None)
        for change, reason in ((dict(protocol='d'*64), 'gateway_identity_mismatch'),
                (dict(operations_port=1), 'gateway_identity_mismatch'),
                (dict(client_policy=None), 'gateway_client_policy_mismatch'),
                (dict(health_status=None), 'gateway_observation_invalid')):
            with self.assertRaises(supervisor.BindingMismatch) as caught:
                supervisor.classify(dict(observation, **change), 'c'*64, 'b'*64,
                                    expected, 18183)
            self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(supervisor.classify(dict(observation, health_status='unhealthy'),
            'c'*64, 'b'*64, expected, 18183), 'BACKEND_UNHEALTHY')


class ProjectionTests(unittest.TestCase):
    def test_fresh_typed_diagnostics_export_without_overwriting_health_or_leaking_text(self):
        root = Path(__file__).resolve().parents[2]
        good = json.loads((root/'docs/evidence/serve-progress-business-20261109/'
            'business-02/execution/ready-2.json').read_text())
        good.update(state='WAIT_DEPENDENCY', business_ready=False,
            gateway_bound_model=0, gateway_binding_reason='gateway_capacity_mismatch',
            gateway_binding_model_generation=good['model_generation'], gateway_binding_age_ms=10)
        projected = native.project_status(good)
        self.assertEqual(projected['gateway_binding']['reason'], 'gateway_capacity_mismatch')
        value = native.empty();value.update(projected)
        self.assertIn('q4t_service_gateway_binding_reason{reason="gateway_capacity_mismatch"} 1',
                      native.metrics(value))
        for change in (dict(gateway_binding_reason='SECRET'),
                       dict(gateway_binding_age_ms=3001),
                       dict(gateway_binding_model_generation=good['model_generation']+1),
                       dict(gateway_binding_age_ms=True)):
            result = native.project_status(dict(good, **change))
            self.assertIsNone(result['gateway_binding'])
            self.assertFalse(result['business_ready'])
        old = dict(good)
        for key in tuple(old):
            if key.startswith('gateway_binding_'):old.pop(key)
        self.assertIsNone(native.project_status(old)['gateway_binding'])
        stale = native.current(value, now=dict(value['sampled'],
            boottime=value['sampled']['boottime']+4))
        self.assertIsNone(stale['gateway_binding'])


class GuardianTests(test_runtime.RuntimeTests):
    def test_typed_rejection_and_legacy_recovery_preserve_all_role_identities(self):
        service = self.launch(models=True, gateway=True, gateway_diagnostics=True)
        first = service.wait(lambda value: value['business_ready'])
        attempts = copy.deepcopy(service.persistent()['attempts'])
        marker = service.directory/'gateway-diagnostic-mode'
        for mode, reason in [('reject', 'gateway_capacity_mismatch'),
                             ('legacy', 'gateway_binding_unavailable')]:
            marker.write_text(mode)
            rejected = service.wait(lambda value: value['reason'] == reason and
                                    not value['business_ready'])
            self.assertTrue(rejected['model_ready'])
            self.assertEqual(rejected['gateway_binding_model_generation'], first['model_generation'])
            self.assertLess(rejected['gateway_binding_age_ms'], 3000)
            for role in ('controller', 'gateway', 'model'):
                self.assertEqual(rejected[role+'_pid'], first[role+'_pid'])
            marker.write_text('')
            recovered = service.wait(lambda value: value['business_ready'])
            self.assertEqual(recovered['gateway_binding_reason'], 'none')
            self.assertEqual(recovered['reason'], '')
        self.assertEqual(service.persistent()['attempts'], attempts)
        self.assertTrue(service.stop()['cleanup_confirmed'])
        service.assert_lease_free()

    def test_stale_and_malformed_diagnostics_cannot_recover_or_revoke_current_model(self):
        service = self.launch(models=True, gateway=True, gateway_diagnostics=True)
        first = service.wait(lambda value: value['business_ready'])
        marker = service.directory/'gateway-diagnostic-mode'
        marker.write_text('malformed')
        bad = service.wait(lambda value: value['auxiliary_projection_errors'] >= 2)
        self.assertTrue(bad['business_ready'])
        self.assertNotIn('PRIVATE_DIAGNOSTIC_MUST_NOT_ESCAPE', json.dumps(bad))
        marker.write_text('stale')
        # Heartbeats continue while stale-generation messages are received.
        later = service.wait(lambda value: value['gateway_binding_age_ms'] >= 150)
        self.assertTrue(later['business_ready'])
        for role in ('controller', 'gateway', 'model'):
            self.assertEqual(later[role+'_pid'], first[role+'_pid'])
            self.assertEqual(later[role+'_generation'], first[role+'_generation'])
        self.assertTrue(service.stop()['cleanup_confirmed'])
        service.assert_lease_free()


if __name__ == '__main__':
    unittest.main()

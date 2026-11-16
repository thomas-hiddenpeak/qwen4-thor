"""Preserve consumed/terminal cycles on root changes; no real launch claims."""
import copy
import unittest
from unittest import mock
import uuid

from tools.serve_recovery import deployment_policy, root_version_policy as transfer
from tools.serve_recovery import test_version_policy, version_launch, version_policy
from tools.serve_recovery import version_registration


class RootVersionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_version_policy.PolicyTests()
        self.fixture.setUp()
        self.saved = self.fixture.pending()
        registry = copy.deepcopy(self.fixture.registry)
        registry.update(root_manifest_sha256='9'*64, cycle_id=str(uuid.uuid4()))
        self.current = dict(version_policy.initial(registry, str(uuid.uuid4())), revision=20)
        self.sid = str(uuid.uuid4())
        self.source = dict(version=1, service_id=self.sid, package='/cpu/new-root',
                           manifest_sha256='9'*64)
        self.deployment = self.intent(self.source, '/cpu/retained-root', '0'*64)

    def intent(self, source, package, digest):
        request = str(uuid.uuid4())
        target = dict(version=2, service_id=self.sid, package=package,
            manifest_sha256=digest, deployment_revision=source.get('deployment_revision', 0)+1,
            deployment_id=request)
        return deployment_policy.prepare(deployment_policy.initial(source), target, request, [])

    def prepare(self, saved=None):
        saved = self.saved if saved is None else saved
        return transfer.prepare(self.deployment, self.current, saved, saved['registry'])

    def test_pending_fallback_restores_original_cycle_and_all_state_except_revision(self):
        value = self.prepare()
        candidate = value['candidate']
        self.assertEqual(candidate, dict(self.saved, revision=21))
        self.assertEqual(version_policy.launch_payload(candidate), 'fallback')
        self.assertTrue(transfer.describe(value)['fallback_consumed'])
        self.assertFalse(transfer.describe(value)['creates_new_cycle'])
        self.assertFalse(transfer.describe(value)['model_actions'])
        transfer.authorized(value, self.current, candidate)

    def test_completed_fallback_and_terminal_protection_survive(self):
        completed = version_policy.finish(self.saved, self.fixture.fallback_proof(), 'd'*64)
        event = dict(self.fixture.event, payload_sha256='c'*64, generation=6)
        terminal = version_policy.observe(completed, event, self.fixture.fallback_proof(),
                                          str(uuid.uuid4()))
        for saved in (completed, terminal):
            value = self.prepare(saved)
            self.assertEqual(value['candidate'], dict(saved, revision=21))
        self.assertTrue(transfer.describe(self.prepare(terminal))['terminal_fault_preserved'])
        # The selector reports the consumed fallback; launch preparation owns
        # the terminal fence and must reject before touching a package/run.
        candidate = self.prepare(terminal)['candidate']
        with mock.patch.object(version_registration, 'load',
                               return_value=dict(state=candidate)), \
                self.assertRaisesRegex(ValueError, 'version recovery exhausted'):
            version_launch.configuration('/CPU_ONLY', {}, '/MUST_NOT_CREATE')

    def test_repeated_back_forward_back_does_not_refund_consumed_fallback(self):
        first = self.prepare()
        backward = first['candidate']
        forward_intent = self.intent(self.deployment['target'], '/cpu/new-root', '9'*64)
        forward = transfer.prepare(forward_intent, backward, self.current,
                                   self.current['registry'])['candidate']
        again_intent = self.intent(forward_intent['target'], '/cpu/retained-root', '0'*64)
        again = transfer.prepare(again_intent, forward, backward,
                                 backward['registry'])['candidate']
        self.assertEqual(again, dict(self.saved, revision=23))
        self.assertEqual(again['switch'], self.saved['switch'])
        self.assertEqual(forward, dict(self.current, revision=22))

    def test_reset_cycle_boot_faults_and_payload_retargeting_are_rejected(self):
        value = self.prepare()
        mutations = [lambda v: v['candidate'].update(switch=None, faults=[]),
            lambda v: v['candidate'].update(boot_id=str(uuid.uuid4())),
            lambda v: v['candidate']['registry'].update(cycle_id=str(uuid.uuid4())),
            lambda v: v['candidate']['registry']['payloads']['fallback'].update(manifest_sha256='f'*64),
            lambda v: v['candidate'].update(revision=20),
            lambda v: v.update(checkpoint_sha256='f'*64)]
        for mutate in mutations:
            invalid = copy.deepcopy(value); mutate(invalid)
            with self.assertRaises(ValueError): transfer.validate(invalid)

    def test_foreign_root_service_registry_and_future_checkpoint_are_rejected(self):
        for which in ('source', 'target', 'service', 'registry', 'future'):
            intent = copy.deepcopy(self.deployment); saved = copy.deepcopy(self.saved)
            registry = copy.deepcopy(saved['registry'])
            if which == 'source': intent['current']['manifest_sha256'] = 'f'*64
            if which == 'target': intent['target']['manifest_sha256'] = 'f'*64
            if which == 'service': intent['target']['service_id'] = str(uuid.uuid4())
            if which == 'registry': registry['qualification_sha256'] = 'f'*64
            if which == 'future': saved['revision'] = 21
            with self.assertRaises(ValueError, msg=which):
                transfer.prepare(intent, self.current, saved, registry)

    def test_unprepared_deployment_and_unrelated_writer_transition_are_rejected(self):
        with self.assertRaises(ValueError):
            transfer.prepare(deployment_policy.initial(self.source), self.current,
                             self.saved, self.saved['registry'])
        value = self.prepare()
        for before, following in [(dict(self.current, revision=19), value['candidate']),
                                  (self.current, dict(value['candidate'], revision=22))]:
            with self.assertRaises(ValueError): transfer.authorized(value, before, following)

    def test_unused_cycle_keeps_its_identity_without_inventing_faults(self):
        value = self.prepare(self.fixture.state)
        self.assertEqual(value['candidate'], dict(self.fixture.state, revision=21))
        self.assertFalse(transfer.describe(value)['fallback_consumed'])
        self.assertEqual(version_policy.launch_payload(value['candidate']), 'primary')

    def test_retry_identity_is_deterministic_and_inputs_are_not_mutated(self):
        before = copy.deepcopy((self.deployment, self.current, self.saved))
        value = self.prepare()
        self.assertEqual(self.prepare(), value)
        self.assertEqual(transfer.request_id(value), transfer.request_id(self.prepare()))
        self.assertEqual(before, (self.deployment, self.current, self.saved))
        other = copy.deepcopy(value)
        request = str(uuid.uuid4())
        other['deployment']['request'] = request
        other['deployment']['target']['deployment_id'] = request
        self.assertNotEqual(transfer.request_id(other), transfer.request_id(value))


if __name__ == '__main__': unittest.main()

"""Outer restart bounds never rewrite core stop, fault or budget authority."""
from copy import deepcopy
import unittest
import uuid

from tools.serve_recovery import ledger, outer_policy as p


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.sid, self.boot = (str(uuid.uuid4()) for _ in range(2))
        self.now = ledger.Stamp(self.boot, ledger.SECOND)
        self.value = p.initial(self.sid, self.now)
        self.core = ledger.new_state(self.sid, self.now)
        self.core['desired'] = 'RUNNING'

    def change(self, op, args):
        old = self.value
        self.value = p.change(old, op, args, self.now)
        self.assertEqual(p.transition(old, self.value), self.value)
        return self.value['last']['result']

    def enable(self): self.change('configure', dict(enabled=True))

    def request(self, **extra):
        args = dict(core=self.core, ticket=str(uuid.uuid4()), manifest_sha256='a'*64,
                    cleanup_verified=True, identity_valid=True, custody=dict(
                        supervisor=dict(pid=100, start_ticks=200, boot_id=self.now.boot_id),
                        instance_token=str(uuid.uuid4()), tree_scope=str(uuid.uuid4())))
        args.update(extra)
        return self.change('request', args)

    def retire(self):
        self.change('retired', dict(ticket=self.value['pending']['ticket'], verified=True))

    def test_disabled_and_stopped_intent_never_charge_or_mutate_core(self):
        original = deepcopy(self.core)
        self.assertEqual(self.request()['reason'], 'disabled')
        self.assertEqual(self.core, original)
        self.enable(); self.core['desired'] = 'STOPPED'; original = deepcopy(self.core)
        self.assertEqual(self.request()['reason'], 'operator_intent')
        self.assertEqual(self.core, original); self.assertEqual(self.value['total'], 0)

    def test_request_binds_original_core_without_resetting_its_budget(self):
        self.enable(); self.core['generations']['model'] = 4
        self.core['attempts']['model'] = [0]
        original = deepcopy(self.core)
        result = self.request(); self.assertTrue(result['allowed'])
        self.assertEqual(self.value['pending']['core_sha256'], ledger.digest(original))
        self.assertEqual(self.value['pending']['core_revision'], original['revision'])
        self.assertEqual(self.value['pending']['core_generations'], original['generations'])
        self.assertEqual(self.value['total'], 1); self.assertEqual(self.core, original)
        self.assertFalse(p.describe(self.value)['device_authorized'])
        self.assertFalse(p.describe(self.value)['model_actions'])

    def test_protection_and_incomplete_runtime_proofs_never_start(self):
        self.enable(); self.core['quarantine'] = 'device_failure'
        self.assertEqual(self.request()['reason'], 'core_protected')
        self.core['quarantine'] = None
        for name in ('cleanup_verified', 'identity_valid'):
            self.assertEqual(self.request(**{name:False})['reason'], 'preflight_incomplete')
        self.assertEqual(self.value['total'], 0)
        with self.assertRaises(ValueError): self.request(nvml_available=True)

    def test_unreconciled_or_lost_dispatch_requires_actual_retirement(self):
        self.enable(); self.assertTrue(self.request()['allowed'])
        ticket = self.value['pending']['ticket']
        self.now = ledger.Stamp(self.boot, 100*ledger.SECOND)
        self.assertEqual(self.request()['reason'], 'unreconciled_attempt')
        with self.assertRaisesRegex(ValueError, 'actual outer cleanup'):
            self.change('retired', dict(ticket=ticket, verified=False))
        self.assertEqual(self.value['total'], 1)
        self.retire(); self.assertTrue(self.request()['allowed'])
        self.assertEqual(self.value['total'], 2)

    def test_owner_is_bound_once_to_original_boot_and_ticket(self):
        self.enable(); self.request(); ticket = self.value['pending']['ticket']
        owner = dict(pid=100, start_ticks=200, boot_id=self.boot)
        with self.assertRaises(ValueError):
            self.change('spawned', dict(ticket=str(uuid.uuid4()), owner=owner))
        with self.assertRaises(ValueError):
            self.change('spawned', dict(ticket=ticket, owner=dict(owner,boot_id=str(uuid.uuid4()))))
        self.change('spawned', dict(ticket=ticket, owner=owner))
        with self.assertRaises(ValueError): self.change('spawned', dict(ticket=ticket, owner=owner))
        self.assertEqual(self.value['pending']['owner'], owner)

    def test_three_starts_latch_protection_across_boot_and_expiry(self):
        self.enable()
        for index in range(3):
            self.now = ledger.Stamp(self.boot, (1+30*index)*ledger.SECOND)
            self.assertTrue(self.request()['allowed']); self.retire()
        self.assertEqual(self.request()['reason'], 'start_limit')
        self.assertTrue(self.value['protected'])
        self.now = ledger.Stamp(str(uuid.uuid4()), 999999*ledger.SECOND)
        self.change('configure', dict(enabled=False)); self.enable()
        self.assertEqual(self.request()['reason'], 'outer_protected')
        self.now = ledger.Stamp(self.now.boot_id, self.now.monotonic_ns+ledger.DAY)
        self.assertEqual(self.request()['reason'], 'outer_protected')
        self.assertEqual(self.value['total'], 3)

    def test_acknowledgement_keeps_charges_and_requires_stopped_disabled(self):
        self.enable()
        for index in range(3):
            self.now = ledger.Stamp(self.boot, (1+30*index)*ledger.SECOND)
            self.request(); self.retire()
        self.request()
        with self.assertRaises(ValueError): self.change('acknowledge', dict(core=self.core))
        self.change('configure', dict(enabled=False)); self.core['desired'] = 'STOPPED'
        starts = deepcopy(self.value['starts']); self.change('acknowledge', dict(core=self.core))
        self.assertEqual(self.value['starts'], starts); self.assertEqual(self.value['total'], 3)
        self.enable(); self.core['desired'] = 'RUNNING'
        self.assertEqual(self.request()['reason'], 'start_limit')

    def test_reboot_does_not_earn_cooldown_or_allow_counter_tampering(self):
        self.enable(); self.request(); self.retire()
        self.now = ledger.Stamp(str(uuid.uuid4()), 999999*ledger.SECOND)
        self.assertEqual(self.request()['reason'], 'cooldown')
        previous = deepcopy(self.value)
        self.now = ledger.Stamp(self.now.boot_id, self.now.monotonic_ns+30*ledger.SECOND)
        self.assertTrue(self.request()['allowed'])
        tampered = deepcopy(self.value); tampered['enabled'] = False
        with self.assertRaises(ValueError): p.transition(previous, tampered)


if __name__ == '__main__': unittest.main()

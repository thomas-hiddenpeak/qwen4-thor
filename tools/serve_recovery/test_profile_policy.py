"""Profile budgets and durable intents; no synthetic model qualification claim."""
import copy
import unittest
import uuid

from tools.serve_recovery import ledger, profile_policy as p
from tools.serve_recovery import test_profile_contract as fixtures


def registry():
    fixture = fixtures.ProfileContractTests()
    fixture.setUp()
    return fixture.fixture()


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.boot = str(uuid.uuid4())
        self.registry = registry()
        self.state = p.initial(self.registry, self.stamp(0))

    def stamp(self, seconds, boot=None):
        return ledger.Stamp(boot or self.boot, seconds*ledger.SECOND)

    def switch(self, state, target, seconds, generation):
        reason = ('sustained_memory_recovery' if target == 'primary'
                  else 'sustained_memory_pressure')
        value = p.prepare(state, target, reason, str(uuid.uuid4()),
                          generation, self.stamp(seconds))
        return p.finish(value, generation+1, value['profiles'][target],
                        self.stamp(seconds+1))

    def test_pending_intent_does_not_claim_completed_profile_or_model_authority(self):
        before = copy.deepcopy(self.state)
        prepared = p.prepare(self.state, 'light', 'cold_memory_capacity',
                             str(uuid.uuid4()), 0, self.stamp(1))
        self.assertEqual(self.state, before)
        self.assertEqual(prepared['selected'], 'primary')
        self.assertEqual(p.selected_for_launch(prepared), 'light')
        self.assertFalse(p.describe(prepared)['model_actions'])
        self.assertEqual(p.permission(prepared, 'primary', self.stamp(86400))
                         ['reason'], 'profile_intent_pending')
        p.transition(self.state, prepared)
        finished = p.finish(prepared, 1, prepared['profiles']['light'], self.stamp(2))
        p.transition(prepared, finished)
        self.assertEqual((finished['selected'], finished['revision']), ('light', 2))

    def test_generation_and_capacity_must_confirm_exact_pending_intention(self):
        prepared = p.prepare(self.state, 'light', 'confirmed_memory_fault',
                             str(uuid.uuid4()), 7, self.stamp(1))
        for generation, digest in [(7, prepared['profiles']['light']),
                                    (True, prepared['profiles']['light']),
                                    (8, prepared['profiles']['primary'])]:
            with self.assertRaises(ValueError):
                p.finish(prepared, generation, digest, self.stamp(2))
        with self.assertRaises(ValueError):
            p.prepare(self.state, 'light', 'sustained_memory_recovery',
                      str(uuid.uuid4()), 0, self.stamp(1))

    def test_controller_and_host_resume_cannot_expire_budgets_or_pending(self):
        prepared = p.prepare(self.state, 'light', 'cold_memory_capacity',
                             str(uuid.uuid4()), 0, self.stamp(1))
        for stamp in [self.stamp(86400), self.stamp(100, str(uuid.uuid4()))]:
            resumed = p.resume(prepared, stamp)
            p.transition(prepared, resumed)
            self.assertEqual(resumed['clock']['logical_ns'], ledger.SECOND)
            for field in ['pending', 'history', 'not_before_ns', 'selected']:
                self.assertEqual(resumed[field], prepared[field])
        light = p.finish(prepared, 1, prepared['profiles']['light'], self.stamp(2))
        resumed = p.resume(light, self.stamp(86400))
        self.assertEqual(p.permission(resumed, 'primary', self.stamp(86400))
                         ['reason'], 'profile_cooldown')
        self.assertEqual(resumed['light_since_ns'], light['light_since_ns'])

    def test_cooldown_residency_hour_and_day_limits_are_independent(self):
        light = self.switch(self.state, 'light', 0, 0)
        self.assertEqual(p.permission(light, 'primary', self.stamp(299))['reason'],
                         'profile_cooldown')
        self.assertEqual(p.permission(light, 'primary', self.stamp(300))['reason'],
                         'minimum_light_residency')
        primary = self.switch(light, 'primary', 301, 1)
        self.assertEqual(p.permission(primary, 'light', self.stamp(601))['reason'],
                         'profile_hour_budget')
        light = self.switch(primary, 'light', 3600, 2)
        primary = self.switch(light, 'primary', 3901, 3)
        self.assertEqual(p.permission(primary, 'light', self.stamp(7500))['reason'],
                         'profile_day_budget')
        self.assertTrue(p.permission(primary, 'light', self.stamp(86400))['allowed'])
        light = self.switch(primary, 'light', 86400, 4)
        self.assertEqual(len(light['history']), 4)

    def test_transition_rejects_budget_history_identity_or_selection_edits(self):
        prepared = p.prepare(self.state, 'light', 'cold_memory_capacity',
                             str(uuid.uuid4()), 0, self.stamp(1))
        for mutate in [lambda v: v.update(registry_sha256='0'*64),
                       lambda v: v['configuration'].update(cooldown_seconds=60),
                       lambda v: v.update(not_before_ns=0),
                       lambda v: v.update(completed_generation=3),
                       lambda v: v.update(revision=99)]:
            value = copy.deepcopy(prepared); mutate(value)
            with self.assertRaises(ValueError): p.transition(self.state, value)
        value = copy.deepcopy(prepared); value['pending'] = None
        with self.assertRaises(ValueError): p.transition(self.state, value)

    def test_invalid_configuration_clock_and_integer_aliases_are_rejected(self):
        for key, value in [('version', True), ('pressure_seconds', 1),
                           ('switches_per_hour', 3), ('switches_per_day', True),
                           ('recover_above_mib', 2049)]:
            with self.assertRaises(ValueError):
                p.configuration(dict(p.DEFAULTS, **{key: value}))
        prepared = p.prepare(self.state, 'light', 'cold_memory_capacity',
                             str(uuid.uuid4()), 0, self.stamp(2))
        for op in [p.advanced, p.resume]:
            with self.assertRaises(ValueError): op(prepared, self.stamp(1))
        self.assertFalse(p.revision(True)); self.assertTrue(p.revision(1, minimum=1))


if __name__ == '__main__': unittest.main()

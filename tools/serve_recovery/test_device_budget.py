"""Cold admission never turns missing visibility or state into a fresh budget."""
import copy
import unittest
import uuid

from tools.serve_recovery import device_budget as p, ledger


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.boot = str(uuid.uuid4())
        self.initial = p.initial(str(uuid.uuid4()), str(uuid.uuid4()), 2002,
                                 self.stamp(1))
        self.state = p.qualify(self.initial, 'a'*64, self.stamp(1))

    def stamp(self, second, boot=None):
        return ledger.Stamp(boot or self.boot, second*ledger.SECOND)

    def charge(self, state, second, boot=None):
        return p.charge(state, 'b'*64, 2049, 1234, str(uuid.uuid4()),
                        self.stamp(second, boot))

    def test_only_current_qualified_boot_can_charge_and_charge_is_not_launch(self):
        with self.assertRaisesRegex(ValueError, 'lacks device qualification'):
            self.charge(self.initial, 1)
        value = self.charge(self.state, 1)
        with self.assertRaisesRegex(ValueError, 'lacks device qualification'):
            self.charge(value, 100000, str(uuid.uuid4()))
        self.assertEqual(value['total'], 1)
        self.assertEqual(value['last_ticket']['expires_ns'], 301*ledger.SECOND)
        self.assertFalse(p.describe(value)['launch_authorized'])
        self.assertFalse(p.describe(value)['model_actions'])

    def test_cooldown_half_hour_and_daily_limits_are_actual_charge_limits(self):
        value = self.charge(self.state, 1)
        with self.assertRaisesRegex(ValueError, 'cooldown'): self.charge(value, 30)
        value = self.charge(value, 31); value = self.charge(value, 61)
        with self.assertRaisesRegex(ValueError, 'budget exhausted'):
            self.charge(value, 1799)
        for second in (1801, 1831, 1861): value = self.charge(value, second)
        with self.assertRaisesRegex(ValueError, 'budget exhausted'):
            self.charge(value, 3601)
        value = self.charge(value, 86401)
        self.assertEqual(value['total'], 7)
        self.assertEqual(len(value['attempts']), 6)
        self.assertEqual(value['attempts'][0]['serial'], 2)

    def test_reboot_and_requalification_preserve_charges_without_downtime_credit(self):
        value = self.state
        for second in (1, 31, 61): value = self.charge(value, second)
        boot = str(uuid.uuid4())
        requalified = p.qualify(value, 'c'*64, self.stamp(1000000, boot))
        self.assertEqual(requalified['attempts'], value['attempts'])
        self.assertEqual(requalified['last_ticket'], value['last_ticket'])
        self.assertEqual(requalified['clock']['logical_ns'], value['clock']['logical_ns'])
        with self.assertRaisesRegex(ValueError, 'budget exhausted'):
            self.charge(requalified, 1000030, boot)
        resumed = self.charge(requalified, 1001740, boot)
        self.assertEqual(resumed['total'], 4)
        self.assertEqual(resumed['last_ticket']['qualification_sha256'], 'c'*64)

    def test_disabled_domain_and_lost_admission_keep_spent_serial(self):
        value = self.charge(self.state, 1)
        disabled = p.disable(value, self.stamp(2))
        self.assertEqual(disabled['last_ticket'], value['last_ticket'])
        with self.assertRaisesRegex(ValueError, 'lacks device qualification'):
            self.charge(disabled, 40)
        resumed = p.qualify(disabled, 'a'*64, self.stamp(40))
        next_charge = self.charge(resumed, 40)
        self.assertEqual(next_charge['total'], 2)
        self.assertNotEqual(next_charge['last_ticket']['id'], value['last_ticket']['id'])
        self.assertEqual(next_charge['attempts'][0], value['attempts'][0])

    def test_transition_rejects_budget_reset_identity_swap_and_new_facts_in_charge(self):
        value = self.charge(self.state, 1)
        candidate = self.charge(value, 31)
        self.assertEqual(p.transition(value, candidate), candidate)
        changes = [dict(total=1), dict(attempts=candidate['attempts'][1:]),
                   dict(service_id=str(uuid.uuid4())), dict(domain_id=str(uuid.uuid4())),
                   dict(runtime_uid=2003), dict(revision=candidate['revision']+1),
                   dict(qualification=dict(boot_id=self.boot, sha256='f'*64))]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                p.transition(value, dict(copy.deepcopy(candidate), **change))

    def test_backwards_clock_overflow_and_noncanonical_receipts_fail_closed(self):
        value = self.charge(self.state, 1)
        with self.assertRaisesRegex(ValueError, 'backwards'):
            p.disable(value, self.stamp(0))
        for change in [dict(expires_ns=0), dict(lease_inode=0),
                       dict(manifest_sha256='z'*64), dict(serial=0)]:
            altered = copy.deepcopy(value); altered['last_ticket'].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                p.validate(altered)
        state = p.qualify(self.initial, 'a'*64,
                         ledger.Stamp(self.boot, ledger.MAX_INTEGER))
        with self.assertRaises(ValueError):
            p.charge(state, 'b'*64, 1, 1, str(uuid.uuid4()),
                     ledger.Stamp(self.boot, ledger.MAX_INTEGER))


if __name__ == '__main__': unittest.main()

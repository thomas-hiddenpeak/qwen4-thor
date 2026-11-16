"""CPU contracts for explicit Chat effort upgrades, never model evidence."""
import copy
import unittest

from tools.serve_recovery import profile_migration as migration
from tools.serve_recovery import profile_policy as policy
from tools.serve_recovery import test_profile_migration as fixtures


class ChatStyleMigrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.MigrationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        old = {n: copy.deepcopy(row['record'])
               for n, row in self.fixture.first.items()}
        for record in old.values():
            record['capabilities']['template']['reasoning_effort'] = [
                'low', 'medium', 'xhigh']
        self.fixture.first = fixtures.bind_records(self.fixture.old, old)
        new = copy.deepcopy(old)
        for record in new.values():
            record['capabilities']['template'].update(
                reasoning_effort=['none', 'minimal', 'low', 'medium',
                                  'high', 'xhigh', 'max'],
                reasoning_effort_default='medium',
                effort_adaptation='native_guidance', none_disables_thinking=True)
        self.records = new
        self.evidence = fixtures.bind_records(self.fixture.new, new)

    def test_explicit_upgrade_preserves_history_and_actual_observations(self):
        fixture = self.fixture
        original = copy.deepcopy((fixture.first, self.evidence))
        grant = fixture.extended_permit(fixture.new, self.evidence,
                                       allow_capability_extension=True)
        self.assertEqual(grant['format'], migration.EXTENSION_FORMAT)
        before = policy.resume(fixture.pending, fixture.stamp(10))
        after = migration.candidate(fixture.pending, grant, fixture.stamp(10))
        for key in before:
            if key != 'registry_sha256':
                self.assertEqual(after[key], before[key], key)
        self.assertNotEqual(after['registry_sha256'], before['registry_sha256'])
        self.assertEqual((fixture.first, self.evidence), original)

    def test_automatic_change_reverse_loss_and_implicit_upgrade_rejected(self):
        fixture = self.fixture
        for flags in ({}, dict(automatic=True, allow_capability_extension=True)):
            with self.assertRaisesRegex(ValueError, 'application capabilities'):
                fixture.extended_permit(fixture.new, self.evidence, **flags)
        with self.assertRaisesRegex(ValueError, 'application capabilities'):
            migration.permit(fixture.new, fixture.old, self.evidence,
                fixture.first, fixture.config, fixture.config,
                allow_capability_extension=True)
        same = copy.deepcopy(fixture.new)
        same['identity']['binary_sha256'] = 'f' * 64
        evidence = fixtures.bind_records(same, copy.deepcopy(self.records))
        grant = migration.permit(fixture.new, same, self.evidence, evidence,
            fixture.config, fixture.config, automatic=True)
        self.assertTrue(grant['automatic'])
        self.assertEqual(grant['format'], migration.FORMAT)

    def test_invalid_enum_or_missing_native_implementation_rejected(self):
        original = self.records['primary']['capabilities']
        mutations = [
            dict(reasoning_effort=['medium', 'unknown']),
            dict(reasoning_effort=['medium', 'medium']),
            dict(reasoning_effort=['medium', True]),
            dict(reasoning_effort=[]),
            dict(reasoning_effort='medium'),
            dict(effort_adaptation='ignored'),
            dict(reasoning_effort_default='xhigh'),
            dict(none_disables_thinking=False),
            dict(none_disables_thinking=1),
        ]
        for fields in mutations:
            with self.subTest(fields=fields):
                changed = copy.deepcopy(original)
                changed['template'].update(fields)
                with self.assertRaises(ValueError):
                    migration.semantic_capabilities(changed)

    def test_enum_order_only_is_semantic_and_unrelated_changes_are_not(self):
        original = self.records['primary']['capabilities']
        changed = copy.deepcopy(original)
        changed['template']['reasoning_effort'].reverse()
        self.assertEqual(migration.semantic_capabilities(original),
                         migration.semantic_capabilities(changed))
        first = migration.semantic_capabilities(original)
        second = copy.deepcopy(first)
        second['template']['reasoning_effort'].pop('low')
        self.assertFalse(migration.extends(first, second))
        first['unreviewed_enum'] = ['a']
        second = copy.deepcopy(first)
        second['unreviewed_enum'].append('b')
        self.assertFalse(migration.extends(first, second))


if __name__ == '__main__':
    unittest.main()

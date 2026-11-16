"""CPU contracts for explicit stop/cache/schema capability delivery."""
import copy
import unittest

from tools.serve_recovery import profile_migration, profile_policy
from tools.serve_recovery import test_profile_migration as previous


class StopMigrationTests(unittest.TestCase):
    def setUp(self):
        self.base = previous.MigrationTests()
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.old = copy.deepcopy(self.base.old)
        records = {name: copy.deepcopy(row['record'])
                   for name, row in self.base.first.items()}
        for record in records.values():
            caps = record['capabilities']
            caps.setdefault('structured_output', {})['available'] = True
            caps['text_stop'] = dict(profile='q4t-text-stop-v1',
                                    state_cache=False, constrained_output=False,
                                    includes_stop_string=False)
        self.first = previous.bind_records(self.old, records)
        records = copy.deepcopy(records)
        self.new = copy.deepcopy(self.old)
        self.new['identity'].update(binary_sha256='e'*64, protocol_sha256='f'*64)
        for record in records.values():
            record['capabilities']['text_stop'].update(
                profile='q4t-text-stop-v2', state_cache=True,
                constrained_output=True,
                constrained_terminal='validate_visible_complete_values_and_receipts',
                private_cache_boundary='complete_prompt_before_generation')
        self.second = previous.bind_records(self.new, records)

    def permit(self, **options):
        return profile_migration.permit(
            self.old, self.new, self.first, self.second,
            self.base.config, self.base.config, **options)

    def test_explicit_only_and_no_feature_loss_on_fallback(self):
        for options in ({}, dict(automatic=True, allow_capability_extension=True)):
            with self.assertRaises(ValueError):
                self.permit(**options)
        self.assertEqual(self.permit(allow_capability_extension=True)['format'],
                         profile_migration.EXTENSION_FORMAT)
        for automatic in (False, True):
            with self.assertRaises(ValueError):
                profile_migration.permit(
                    self.new, self.old, self.second, self.first,
                    self.base.config, self.base.config, automatic=automatic,
                    allow_capability_extension=True)
        self.assertTrue(profile_migration.permit(
            self.new, self.new, self.second, self.second, self.base.config,
            self.base.config, automatic=True)['automatic'])

    def test_pending_history_and_consumed_budgets_survive(self):
        state = profile_policy.initial(self.old, self.base.stamp(0), self.base.config)
        state = profile_policy.prepare(state, 'light', 'sustained_memory_pressure',
                                       str(previous.uuid.uuid4()), 7,
                                       self.base.stamp(1))
        grant = self.permit(allow_capability_extension=True)
        after = profile_migration.candidate(state, grant, self.base.stamp(1))
        for key in ('pending', 'history', 'selected', 'not_before_ns',
                    'light_since_ns', 'clock', 'configuration', 'profiles'):
            self.assertEqual(after[key], state[key])

    def test_complete_contract_boolean_types_and_backend_consistency(self):
        source = self.second['primary']['record']['capabilities']
        saved = copy.deepcopy(source)
        for change in (
            lambda c: c['text_stop'].update(state_cache=1),
            lambda c: c['text_stop'].update(state_cache=False),
            lambda c: c['text_stop'].update(constrained_output=1),
            lambda c: c['text_stop'].update(private_cache_boundary='generated_tail'),
            lambda c: c['text_stop'].update(constrained_terminal='skip_validation'),
            lambda c: c['text_stop'].update(profile='q4t-text-stop-v1'),
            lambda c: c['structured_output'].update(available=False),
            lambda c: c.update(structured_output=[])):
            value = copy.deepcopy(source)
            change(value)
            with self.assertRaises(ValueError):
                profile_migration.semantic_capabilities(value)
        profile_migration.semantic_capabilities(source)
        self.assertEqual(source, saved)

    def test_unrelated_stop_semantics_cannot_change_as_extension(self):
        records = {name: copy.deepcopy(row['record'])
                   for name, row in self.second.items()}
        for record in records.values():
            record['capabilities']['text_stop']['includes_stop_string'] = True
        changed = copy.deepcopy(self.new)
        evidence = previous.bind_records(changed, records)
        with self.assertRaises(ValueError):
            profile_migration.permit(
                self.old, changed, self.first, evidence, self.base.config,
                self.base.config, allow_capability_extension=True)


if __name__ == '__main__':
    unittest.main()

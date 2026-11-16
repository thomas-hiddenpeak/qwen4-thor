"""MCP capability upgrades and automatic recovery authority; CPU fixtures only."""
import copy
import unittest

from tools.serve_recovery import profile_migration as migration
from tools.serve_recovery import test_profile_migration as profile_fixture
from tools.serve_recovery import test_version_registration as version_fixture
from tools.serve_recovery import version_contract


def mcp_capability():
    return dict(profile='q4t-native-mcp-v1', enabled=True,
        approval_default='always', authenticated_continuation=True,
        catalog_filtering=True, input_output_schemas=True,
        content_and_resources=True, frozen_media_replay=True,
        encrypted_session_state=True, automatic_tool_replay=False,
        model_restart_on_error=False, core_readiness_dependency=False)


class McpMigrationTests(unittest.TestCase):
    def setUp(self):
        self.base = profile_fixture.MigrationTests()
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.records, _ = self.base.search_extension()
        for record in self.records.values():
            response = record['capabilities']['responses']
            response['output_items'] += ['mcp_list_tools', 'mcp_approval_request', 'mcp_call']
            response['mcp'] = mcp_capability()
        self.evidence = profile_fixture.bind_records(self.base.new, self.records)

    def test_explicit_mcp_upgrade_preserves_history_and_cannot_hide_feature_loss(self):
        base = self.base
        original = copy.deepcopy((base.first, self.evidence, base.pending))
        grant = base.extended_permit(base.new, self.evidence, allow_capability_extension=True)
        after = migration.candidate(base.pending, grant, base.stamp(999999))
        for key in ('history', 'pending', 'not_before_ns', 'selected', 'profiles',
                    'completed_generation', 'light_since_ns'):
            self.assertEqual(after[key], base.pending[key])
        for automatic in (False, True):
            with self.assertRaisesRegex(ValueError, 'application capabilities'):
                base.extended_permit(base.new, self.evidence, automatic=automatic)
        with self.assertRaisesRegex(ValueError, 'application capabilities'):
            migration.permit(base.new, base.old, self.evidence, base.first,
                base.config, base.config, allow_capability_extension=True)
        self.assertEqual((base.first, self.evidence, base.pending), original)

    def test_recovery_keeps_mcp_contract_and_original_evidence(self):
        base = self.base
        previous = profile_fixture.bind_records(base.old, copy.deepcopy(self.records))
        original = copy.deepcopy((previous, self.evidence))
        grant = migration.permit(base.old, base.new, previous, self.evidence,
                                 base.config, base.config, automatic=True)
        self.assertEqual(grant['format'], migration.FORMAT)
        self.assertEqual((previous, self.evidence), original)
        changed = copy.deepcopy(self.records)
        for record in changed.values():
            record['capabilities']['responses']['mcp']['session_affinity'] = False
        # Unrecognized/new promises still compare exactly; the normalized enum
        # cannot erase a change elsewhere in the contract.
        altered = profile_fixture.bind_records(base.new, changed)
        with self.assertRaisesRegex(ValueError, 'application capabilities'):
            migration.permit(base.old, base.new, previous, altered,
                             base.config, base.config, automatic=True)

    def test_mcp_outputs_require_approval_isolation_and_complete_items(self):
        original = self.records['primary']['capabilities']
        mutations = [lambda r:r.pop('mcp'),
            lambda r:r['output_items'].remove('mcp_approval_request'),
            lambda r:r['output_items'].append('mcp_call'),
            lambda r:r['mcp'].update(profile='unknown'),
            lambda r:r['mcp'].update(approval_default='never')]
        for name in ('enabled', 'authenticated_continuation', 'catalog_filtering',
                     'input_output_schemas', 'content_and_resources',
                     'frozen_media_replay', 'encrypted_session_state'):
            mutations.append(lambda r, n=name:r['mcp'].update({n:1}))
        for name in ('automatic_tool_replay', 'model_restart_on_error',
                     'core_readiness_dependency'):
            mutations.append(lambda r, n=name:r['mcp'].update({n:True}))
        for mutate in mutations:
            changed = copy.deepcopy(original)
            mutate(changed['responses'])
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                migration.semantic_capabilities(changed)


class PrivatePolicyRecoveryTests(unittest.TestCase):
    def test_sealed_authority_cannot_change_or_disappear_on_automatic_recovery(self):
        fixture = version_fixture.RegistrationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        first = version_contract.payload(fixture.root)
        second = version_contract.payload(fixture.fallback)
        version_contract.compatible(first, second)
        for key, filename in (
                ('access_program_policy', 'access-program-policy.json'),
                ('file_input_policy', 'file-input-policy.json'),
                ('mcp_policy', 'mcp-policy.json'),
                ('web_policy', 'web-policy.json')):
            a, b = copy.deepcopy(first), copy.deepcopy(second)
            for value in (a, b):
                value['engine'][key] = dict(profile='cpu-authority', sha256='a'*64)
                value['engine']['files'][filename] = 'a'*64
            version_contract.compatible(a, b)
            for mutation in (
                    lambda v:v['engine'].pop(key),
                    lambda v:v['engine'][key].update(sha256='b'*64),
                    lambda v:v['engine']['files'].pop(filename),
                    lambda v:v['engine']['files'].update({filename:'b'*64})):
                changed = copy.deepcopy(b)
                mutation(changed)
                with self.subTest(policy=key), self.assertRaisesRegex(
                        ValueError, 'private application policy'):
                    version_contract.compatible(a, changed)
                with self.assertRaisesRegex(ValueError, 'private application policy'):
                    version_contract.compatible(changed, a)
        self.assertFalse(version_fixture.manager.present(fixture.state))
        self.assertEqual((fixture.state/'ledger.json').read_bytes(), fixture.core_before)


class ProviderMigrationTests(unittest.TestCase):
    setUp = McpMigrationTests.setUp

    def provider_records(self, enabled):
        records = copy.deepcopy(self.records)
        for record in records.values():
            mcp = record['capabilities']['responses']['mcp']
            mcp.update(native_provider_adapters=enabled,
                connector_transport='native_or_deployment_mapped_mcp' if enabled
                    else 'deployment_mapped_mcp')
            if enabled:
                mcp['native_provider_ids'] = ['connector_googlecalendar']
        return records

    def test_provider_upgrade_is_explicit_and_preserves_original_evidence(self):
        base = self.base
        old = profile_fixture.bind_records(base.old, self.provider_records(False))
        new = profile_fixture.bind_records(base.new, self.provider_records(True))
        before = copy.deepcopy((old, new))
        grant = migration.permit(base.old, base.new, old, new, base.config,
            base.config, allow_capability_extension=True)
        self.assertEqual(grant['format'], migration.EXTENSION_FORMAT)
        self.assertEqual((old, new), before)
        for automatic in (False, True):
            with self.assertRaisesRegex(ValueError, 'application capabilities'):
                migration.permit(base.old, base.new, old, new, base.config,
                    base.config, automatic=automatic)
        with self.assertRaisesRegex(ValueError, 'application capabilities'):
            migration.permit(base.new, base.old, new, old, base.config,
                base.config, allow_capability_extension=True)

    def test_provider_recovery_requires_same_exact_provider_set(self):
        base = self.base
        records = self.provider_records(True)
        old = profile_fixture.bind_records(base.old, copy.deepcopy(records))
        new = profile_fixture.bind_records(base.new, copy.deepcopy(records))
        migration.permit(base.old, base.new, old, new, base.config, base.config,
                         automatic=True)
        changed = copy.deepcopy(records)
        for record in changed.values():
            record['capabilities']['responses']['mcp']['native_provider_ids'].append(
                'connector_gmail')
        expanded = profile_fixture.bind_records(base.new, changed)
        with self.assertRaisesRegex(ValueError, 'application capabilities'):
            migration.permit(base.old, base.new, old, expanded, base.config,
                base.config, automatic=True, allow_capability_extension=True)
        migration.permit(base.old, base.new, old, expanded, base.config,
            base.config, allow_capability_extension=True)

    def test_provider_capability_types_and_catalog_are_not_relaxed(self):
        original = self.provider_records(True)['primary']['capabilities']
        for patch in (dict(native_provider_adapters=1),
                dict(native_provider_ids=[]), dict(native_provider_ids=['unknown']),
                dict(native_provider_ids=['connector_googlecalendar']*2),
                dict(native_provider_ids=[True]), dict(connector_transport='http')):
            changed = copy.deepcopy(original)
            changed['responses']['mcp'].update(patch)
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                migration.semantic_capabilities(changed)


if __name__ == '__main__':
    unittest.main()

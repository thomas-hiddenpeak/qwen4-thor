"""Native web qualification upgrades and capability-preserving recovery."""
import copy
import unittest

from tools.serve_recovery import profile_migration as migration
from tools.serve_recovery import test_profile_migration as fixture


def capabilities():
    chat = dict(profile='q4t-native-web-search-v1', chat_options='web_search_options',
        pre_answer_search=True, json=True, stream=True, url_citations=True,
        stored=True, batch_frozen_inputs=True, policy_check_cli=True,
        policy_profile='q4t-web-deployment-policy-v1', configuration_endpoint='/v1/config',
        configuration_field='web_deployment', external_program_required=False,
        request_override=False, core_readiness_dependency=False, model_restart_on_error=False)
    web = dict(profile='q4t-native-web-search-v1', implemented=True,
        executor='native_configured_sites', actions=['search', 'open_page', 'find_in_page'],
        offline_index=True, filters=['allowed_domains', 'blocked_domains'],
        user_location=True, search_context_size=True, search_content_types=['text', 'image'],
        image_settings=True, return_token_budget=True, max_tool_calls=True,
        shared_output_budget=True, shared_acquisition_budget=True,
        source_include='web_search_call.action.sources',
        citation='actual_source_marker_in_generated_text', citation_offsets='unicode_scalar',
        private_history=True, background=True, batch=True, history_replays_search=False,
        runtime_status='/v1/config.web_deployment', core_readiness_dependency=False,
        model_restart_on_error=False)
    return chat, web


class WebMigrationTests(unittest.TestCase):
    def setUp(self):
        self.base = fixture.MigrationTests()
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.records, _ = self.base.search_extension()
        chat, web = capabilities()
        for record in self.records.values():
            caps = record['capabilities']
            caps['web_search'] = copy.deepcopy(chat)
            caps['responses']['web_search'] = copy.deepcopy(web)
            caps['responses']['output_items'].append('web_search_call')
        self.evidence = fixture.bind_records(self.base.new, self.records)

    def test_explicit_upgrade_only_and_no_capability_loss_on_fallback(self):
        base = self.base
        original = copy.deepcopy((base.first, self.evidence))
        for options in ({}, dict(automatic=True, allow_capability_extension=True)):
            with self.assertRaises(ValueError):
                base.extended_permit(base.new, self.evidence, **options)
        grant = base.extended_permit(base.new, self.evidence, allow_capability_extension=True)
        self.assertEqual(grant['format'], migration.EXTENSION_FORMAT)
        for automatic in (False, True):
            with self.assertRaises(ValueError):
                migration.permit(base.new, base.old, self.evidence, base.first,
                    base.config, base.config, automatic=automatic,
                    allow_capability_extension=True)
        self.assertEqual((base.first, self.evidence), original)

    def test_optional_web_provider_requires_both_native_clients_and_explicit_upgrade(self):
        previous = self.records['primary']['capabilities']
        modern = copy.deepcopy(previous)
        modern['web_search']['optional_providers'] = {'mwmbl_v2': True}
        with self.assertRaises(ValueError):
            migration.semantic_capabilities(modern)
        modern['responses']['web_search']['optional_providers'] = {'mwmbl_v2': 1}
        with self.assertRaises(ValueError):
            migration.semantic_capabilities(modern)
        modern['responses']['web_search']['optional_providers'] = {'mwmbl_v2': True}
        old = migration.semantic_capabilities(previous)
        new = migration.semantic_capabilities(modern)
        self.assertTrue(migration.explicit_extension(old, new))
        self.assertFalse(migration.explicit_extension(new, old))
        self.assertNotEqual(old, new)

    def test_pending_history_and_consumed_budget_survive_upgrade(self):
        base = self.base
        grant = base.extended_permit(base.new, self.evidence, allow_capability_extension=True)
        pending = copy.deepcopy(base.pending)
        after = migration.candidate(pending, grant, base.stamp(999999))
        for key in ('history', 'pending', 'not_before_ns', 'selected', 'profiles',
                    'completed_generation', 'light_since_ns', 'configuration'):
            self.assertEqual(after[key], pending[key])
        self.assertEqual(pending, base.pending)

    def test_complete_execution_citation_and_isolation_contract_required(self):
        source = self.records['primary']['capabilities']
        original = copy.deepcopy(source)
        mutations = [lambda c:c.pop('web_search'),
            lambda c:c['responses'].pop('web_search'),
            lambda c:c['responses']['output_items'].remove('web_search_call'),
            lambda c:c['responses'].update(output_items=None),
            lambda c:c['responses']['output_items'].append('web_search_call'),
            lambda c:c['responses']['web_search'].update(executor='external_daemon'),
            lambda c:c['responses']['web_search'].update(citation='guessed_url'),
            lambda c:c['responses']['web_search'].update(citation_offsets='utf8_bytes'),
            lambda c:c['responses']['web_search'].update(actions=['search', 'search']),
            lambda c:c['web_search'].update(policy_profile='unknown')]
        for container, record in zip(('chat', 'responses'), capabilities()):
            for name, value in record.items():
                if type(value) is bool:
                    def alter(c, container=container, name=name, value=value):
                        target = c['web_search'] if container == 'chat' else \
                            c['responses']['web_search']
                        target[name] = 1 if value else True
                    mutations.append(alter)
        for change in mutations:
            bad = copy.deepcopy(source)
            change(bad)
            with self.assertRaises(ValueError):
                migration.semantic_capabilities(bad)
        migration.semantic_capabilities(source)
        self.assertEqual(source, original)

    def test_equal_qualified_capabilities_recover_without_hiding_other_changes(self):
        base = self.base
        previous = fixture.bind_records(base.old, copy.deepcopy(self.records))
        self.assertTrue(migration.permit(base.old, base.new, previous, self.evidence,
                                        base.config, base.config, automatic=True)['automatic'])
        changed = copy.deepcopy(self.records)
        for record in changed.values():
            record['capabilities']['responses']['web_search']['extra_coverage'] = 'all_web'
        new = copy.deepcopy(base.new)
        altered = fixture.bind_records(new, changed)
        with self.assertRaises(ValueError):
            migration.permit(base.old, new, previous, altered,
                             base.config, base.config, automatic=True)


if __name__ == '__main__':
    unittest.main()

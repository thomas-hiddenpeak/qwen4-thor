"""Reject substituted historical messages, cursors and refusal text at handoff."""
import copy
import unittest

from tools.serve_chat_storage import refusal_profiles


class StoredRefusalHandoffTests(unittest.TestCase):
    def setUp(self):
        self.original = {'sdk': '7.47.0', 'cases': []}
        for i, (part, field) in enumerate([
                ('未授权修改项目资料。', '未授权修改项目资料。'),
                ('原始拒答片段。', '单独记录的拒答说明。')]):
            self.original['cases'].append(dict(
                id=f'chatcmpl_{i}', message_id=f'chatmsg_{i}', last_id=f'last_{i}',
                original_content_parts=[{'type': 'refusal', 'refusal': part}],
                refusal=field, peer=404, recovered=False))
        self.restored = copy.deepcopy(self.original)
        for row in self.restored['cases']:
            row['recovered'] = True

    def test_restored_page_must_retain_original_fields_and_identity(self):
        refusal_profiles.check_result(self.original)
        refusal_profiles.check_result(self.restored, self.original)
        for key in ('id', 'message_id', 'last_id', 'refusal', 'original_content_parts'):
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                changed = copy.deepcopy(self.restored)
                changed['cases'][1][key] = changed['cases'][0][key]
                refusal_profiles.check_result(changed, self.original)

    def test_tenant_and_recovery_observations_are_required(self):
        for key, value in [('peer', 200), ('recovered', False)]:
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                changed = copy.deepcopy(self.restored)
                changed['cases'][1][key] = value
                refusal_profiles.check_result(changed, self.original)

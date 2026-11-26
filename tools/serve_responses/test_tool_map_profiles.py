import copy
import unittest

from tools.serve_recovery import profile_live_reuse as reuse
from tools.serve_responses import chatkit_actions_profiles as handoffs
from tools.serve_responses import tool_map_profiles as maps
from tools.serve_service import package


class ToolMapHandoff(unittest.TestCase):
    def setUp(self):
        self.first = self.observation('1')
        self.next = self.observation('2')
        self.next['restored'] = copy.deepcopy(self.first['saved'])

    def observation(self, digit):
        row = package.read(maps.PROOF)['sdk']
        row['call_id'] = 'call_' + digit * 32
        row['saved'] = dict(agent_id='agent_' + digit*32, chat_id='chatcmpl-auto-' + digit*32,
                            call_id=row['call_id'], chat_sha256=digit*64,
                            agent_fields_sha256=digit*64, owner_patch=200, foreign=404)
        row['restored'] = None
        return row

    def test_recognizes_issued_predecessor(self):
        spec = handoffs.handoff({maps.KEY: {}})
        self.assertEqual((spec.key, spec.predecessor, spec.reviewed),
                         (maps.KEY, reuse.REGION_RECEIPT, reuse.REGION_REVIEWED))
        self.assertEqual(reuse.ACCEPTANCE_LABELS[reuse.TOOL_MAP_RECORD],
                         'ACCEPT_TOOL_MAP_PRIMARY_LIGHT')
        maps.check(self.first)
        maps.check(self.next, self.first)

    def test_wrong_saved_ids_and_cross_client_disclosure_fail(self):
        for name, value in [('chat_sha256', '3'*64), ('agent_fields_sha256', '3'*64),
                            ('agent_id', self.next['saved']['agent_id']), ('foreign', 200)]:
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                changed = copy.deepcopy(self.next)
                changed['restored'][name] = value
                maps.check(changed, self.first)

    def test_rejects_different_sources_types_and_terminal(self):
        for name in ('revision', 'source', 'type', 'terminal', 'answer', 'reused'):
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                changed = copy.deepcopy(self.next)
                if name == 'revision': changed['arguments']['versions']['primary'] += 1
                if name == 'type': changed['arguments']['versions']['primary'] = float(changed['arguments']['versions']['primary'])
                if name == 'source': changed['records']['primary']['source_sha256'] = '0'*64
                if name == 'terminal': changed['finish_reasons'] = ['stop']
                if name == 'answer': changed['reply'] = '0'
                if name == 'reused': changed['saved']['chat_id'] = self.first['saved']['chat_id']
                maps.check(changed, self.first)

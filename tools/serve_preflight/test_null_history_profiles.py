import copy
import unittest

from tools.serve_preflight import null_history_profiles as history
from tools.serve_recovery import profile_live_reuse as reuse
from tools.serve_responses import chatkit_actions_profiles
from tools.serve_service import package


class NullHistoryHandoff(unittest.TestCase):
    def setUp(self):
        self.original = package.read(history.PROOF)['client']
        self.next = copy.deepcopy(self.original)
        self.next['restored'] = copy.deepcopy(self.original['saved'])
        self.next['saved'].update(chat_id='chatcmpl-auto-'+'1'*32,
                                  call_id='call_'+'2'*32, empty_id='chatmsg_'+'3'*64)

    def test_original_and_recovered_history(self):
        history.check(self.original)
        history.check(self.next, self.original)
        spec = chatkit_actions_profiles.handoff({history.KEY: {}})
        self.assertEqual(spec.reviewed, reuse.TOOL_MAP_REVIEWED)
        self.assertEqual(spec.predecessor, reuse.TOOL_MAP_RECEIPT)
        self.assertEqual(reuse.ACCEPTANCE_LABELS[reuse.NULL_HISTORY_RECORD],
                         'ACCEPT_NULL_ASSISTANT_HISTORY_PRIMARY_LIGHT')

    def test_refuses_changed_original(self):
        for key, value in (('chat_id','chatcmpl-other'), ('empty_id','chatmsg_'+'7'*64),
                           ('history_sha256','8'*64), ('strict_empty_page',False), ('peer',200)):
            with self.subTest(field=key), self.assertRaises(RuntimeError):
                row = copy.deepcopy(self.next)
                row['restored'][key] = value
                history.check(row, self.original)

    def test_requires_real_lookup_and_stream(self):
        for key, value in (('reply','12'), ('finishes',['length']), ('usage',True),
                           ('revision',536.0), ('source_sha256','f'*64), ('chunks',1)):
            with self.subTest(field=key), self.assertRaises(RuntimeError):
                row = copy.deepcopy(self.next)
                row[key] = value
                history.check(row, self.original)

    def test_rejects_reused_new_ids(self):
        for key in ('call_id','chat_id','empty_id'):
            with self.subTest(field=key), self.assertRaises(RuntimeError):
                row = copy.deepcopy(self.next)
                row['saved'][key] = self.original['saved'][key]
                history.check(row, self.original)

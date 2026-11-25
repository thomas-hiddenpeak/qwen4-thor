from copy import deepcopy
import unittest

from tools.serve_responses import chatkit_retry_profiles as profiles
from tools.serve_service import package


class RetryHandoff(unittest.TestCase):
    def setUp(self):
        actual = package.read(profiles.PROOF)['sdk']
        source = dict(workflow='agent_fixture', session='cksess_fixture', attachment='file_fixture',
                      call=actual['original_call'], thread=actual['thread'], source_deleted=404,
                      proposed_count=actual['verified_count'], sha256=actual['source_sha256'],
                      bytes=profiles.SCOPE.stat().st_size)
        self.first = dict(sdk=actual['sdk'], phase='primary', source=source,
                          original_ids=actual['old_items'][:2], browser_zero=2,
                          management_zero=0, peer=404, deleted=404, chat='42')
        self.second = dict(deepcopy(self.first), phase='light',
                           recovered=dict(call=actual['retry_call'], ids=actual['final_items'],
                                          answer=actual['answer'], foreign_browser=404))

    def test_retains_user_id_and_changes_tool_call(self):
        profiles.check_result(self.first)
        profiles.check_result(self.second, self.first)
        for key, bad in [('answer','wrong'), ('call',self.first['source']['call']),
                         ('ids',self.first['original_ids']), ('foreign_browser',200)]:
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                row = deepcopy(self.second)
                row['recovered'][key] = bad
                profiles.check_result(row, self.first)

    def test_rejects_a_different_source_or_tenant(self):
        for key, bad in [('peer',200), ('deleted',200), ('browser_zero',0), ('management_zero',2)]:
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                profiles.check_result(dict(self.first, **{key:bad}))
        changed = deepcopy(self.second)
        changed['source']['attachment'] = 'file_other'
        with self.assertRaises(RuntimeError):
            profiles.check_result(changed, self.first)

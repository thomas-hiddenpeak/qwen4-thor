"""Check the semantic boundary of the retained typed SDK conversations."""
import copy
import unittest

from tools.serve_protocol import typed_history_profiles as history
from tools.serve_service import package


class TypedHistoryRecovery(unittest.TestCase):
    def setUp(self):
        self.original = package.read(history.PROOF)['sdk']
        self.restored = copy.deepcopy(self.original)
        for case in self.restored['cases']:
            case['recovered_from'] = case['id']
            case['id'] += '-replayed'

    def test_original_and_recovered_transports(self):
        history.check_result(self.original)
        history.check_result(self.restored, self.original)

    def test_swapped_type_collapsed_history_and_wrong_document_count(self):
        cases = self.original['cases']
        for field, wrong in [('arguments', cases[0]['arguments']),
                             ('answer', cases[0]['answer']),
                             ('answer', dict(cases[1]['answer'], required_paths=0)),
                             ('terminal', 'length'), ('prompt_tokens', 0)]:
            with self.subTest(field=field, wrong=wrong), self.assertRaises(RuntimeError):
                row = copy.deepcopy(self.original)
                row['cases'][1][field] = wrong
                history.check_result(row)

    def test_wrong_prior_identity_and_reused_completion(self):
        for key, value in [('recovered_from', self.original['cases'][0]['id']),
                           ('id', self.original['cases'][1]['id']),
                           ('call_id', self.original['cases'][1]['call_id']+'-other')]:
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                row = copy.deepcopy(self.restored)
                row['cases'][1][key] = value
                history.check_result(row, self.original)

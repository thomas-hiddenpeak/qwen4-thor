"""Exercise the visual profile acceptance checks using an actual TLS transcript."""
from copy import deepcopy
import unittest

from tools.serve_recovery import ledger, profile_live_reuse as reuse, profile_record
from tools.serve_responses import visual_profiles as visual
from tools.serve_service import package


class VisualProfileTests(unittest.TestCase):
    def setUp(self):
        self.first = dict(package.read(visual.PROOF)['sdk'], restored=[])
        self.next = deepcopy(self.first)
        for n, (prior, current) in enumerate(zip(
                [*self.first['observations'], self.first['refusal']],
                [*self.next['observations'], self.next['refusal']]), start=1):
            self.next['restored'].append(dict(id=prior['id'], content=deepcopy(prior['content']), peer=404))
            current['id'] = 'resp_' + format(n, '032x')

    def test_visual_restore_checks_original_content_and_owner(self):
        visual.check(self.next, self.first)
        for key, value in [('peer', 200), ('id', self.next['observations'][0]['id']),
                           ('content', self.first['refusal']['content'])]:
            row = deepcopy(self.next)
            row['restored'][0][key] = value
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                visual.check(row, self.first)

    def test_visual_restore_checks_fresh_output_and_refusal(self):
        for index, key, value in [(0, 'content', self.first['refusal']['content']),
                                  (1, 'streamed_text', 'Blue'), (1, 'refusal_delta', True),
                                  (1, 'content', [dict(type='output_text', text='green!!!')])]:
            row = deepcopy(self.next)
            row['observations'][index][key] = value
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                visual.check(row, self.first)
        row = deepcopy(self.next)
        row['refusal']['content'] = self.first['observations'][0]['content']
        with self.assertRaises(RuntimeError):
            visual.check(row, self.first)
        row = deepcopy(self.next)
        row['invalid'][1]['param'] = 'prompt'
        with self.assertRaises(RuntimeError):
            visual.check(row, self.first)

    def test_new_record_requires_its_own_history_format(self):
        record = dict(format=reuse.VISUAL_ANSWER_RECORD,
                      reused_business=dict(format=reuse.VISUAL_ANSWER_FORMAT))
        profile_record.shape(record, profile_record.LEGACY_BYTES + 1)
        record['reused_business']['format'] = reuse.MEDIA_HANDOFF_FORMAT
        with self.assertRaises(ledger.LedgerError):
            profile_record.shape(record, profile_record.LEGACY_BYTES + 1)


if __name__ == '__main__':
    unittest.main()

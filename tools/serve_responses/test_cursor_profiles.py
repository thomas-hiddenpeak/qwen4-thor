import copy
import unittest

from tools.serve_responses import cursor_profiles


class CursorProfileHandoff(unittest.TestCase):
    def setUp(self):
        store, file, batch = ('vs_'+'1'*32, 'file_'+'2'*32, 'vsfb_'+'3'*32)
        self.primary = dict(sdk='7.47.0', store=store, file=file, batch=batch, foreign_404=2, requests=15, upload_probes=1,
            pages=dict(batches=['', ''], consents=[None, None], peer_stores=['', ''],
                       owned_stores=[store, store], files=[file, file], members=[file, file],
                       filtered_files=['', ''], filtered_members=['', ''],
                       initial_stores=['', ''], initial_files=['', '']))
        self.light = copy.deepcopy(self.primary)
        self.light.update(requests=10, upload_probes=0)
        for name in ('initial_stores', 'initial_files'):
            del self.light['pages'][name]

    def test_original_store_batch_and_file_reopen_across_models(self):
        cursor_profiles.check(self.light, 'light', self.primary)
        for field, prefix in [('store', 'vs'), ('file', 'file'), ('batch', 'vsfb')]:
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                cursor_profiles.check(dict(self.light, **{field:prefix+'_'+'a'*32}),
                                      'light', self.primary)

    def test_cursor_contract_and_owner_protection_on_both_sides(self):
        for name, wrong in [('batches', [None, None]), ('consents', ['', '']),
                            ('filtered_members', ['foreign', '']), ('members', ['', ''])]:
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                changed = copy.deepcopy(self.primary)
                changed['pages'][name] = wrong
                cursor_profiles.check(self.light, 'light', changed)
        with self.assertRaises(RuntimeError):
            cursor_profiles.check(dict(self.primary, upload_probes=2), 'primary')
        with self.assertRaises(RuntimeError):
            cursor_profiles.check(dict(self.light, foreign_404=1), 'light', self.primary)

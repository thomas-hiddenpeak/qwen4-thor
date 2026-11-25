"""Run identity and page receipts required for a capacity handoff."""
import copy
import unittest

from tools.serve_responses import assistant_pagination_profiles as pages


class PaginationHandoff(unittest.TestCase):
    def setUp(self):
        self.paths = ['/assistants', '/threads/{thread_id}/messages',
                      '/threads/{thread_id}/runs', '/threads/{thread_id}/runs/{run_id}/steps']
        ids = [['asst_1'], ['msg_1', 'msg_2'], ['run_1'], ['step_1']]
        first = [self.paths[0], '/chatkit/threads', *self.paths[1:3]]
        rounds = [path for path in self.paths for _ in range(2)]
        rows = [dict(path=path, method='GET', status=200, json_sha256=f'{i:064x}')
                for i,path in enumerate(first+rounds)]
        self.first = dict(sdk='7.47.0', answer='42', independent_chat='42', foreign_status=404,
                          pages=dict(zip(self.paths, ids)), assistant='asst_1', thread='thread_1',
                          run='run_1', receipts=rows)
        self.after = dict(self.first, receipts=[rows[1], *rows[-8:]])

    def test_recovered_original(self):
        pages.check_result(self.first)
        pages.check_result(self.after, self.first)

    def test_rejects_substitution_in_each_identity_or_wire_receipt(self):
        for name in ('assistant', 'run', 'thread', 'pages', 'receipts', 'foreign_status', 'independent_chat'):
            with self.subTest(name=name):
                row = copy.deepcopy(self.after)
                if name == 'receipts':
                    row[name][-1]['json_sha256'] = 'a'*64
                elif name == 'pages':
                    row[name][self.paths[1]].reverse()
                else:
                    row[name] = 'changed'
                with self.assertRaises(RuntimeError):
                    pages.check_result(row, self.first)

    def test_rejects_missing_or_unsuccessful_page(self):
        no_runs = dict(self.first, receipts=[r for r in self.first['receipts'] if r['path'] != self.paths[2]])
        for r in [no_runs, dict(self.first, receipts=[dict(self.first['receipts'][0], status=204),
                                                     *self.first['receipts'][1:]])]:
            with self.assertRaises(RuntimeError):
                pages.check_result(r)

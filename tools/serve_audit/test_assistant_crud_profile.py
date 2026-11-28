"""Recheck wire evidence and reject counterexamples to resource ownership and identity."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest

from tools.serve_audit.assistant_crud_evidence import read_clients
from tools.serve_audit.assistant_crud_profile import verify


class AssistantCrudReceiptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        evidence = Path(__file__).resolve().parents[2] / 'docs/evidence'
        cls.source = (evidence/'serve-completion-best-of-20261128/delivery.json').read_bytes()
        cls.path = evidence/'serve-assistants-crud-sdk-20261128'
        cls.label = 'python-2.8.1'
        cls.data = json.loads((cls.path/(cls.label+'.json')).read_bytes())

    def test_all_clients_match_their_wire_receipts(self):
        summary = json.loads((self.path/'cpu.json').read_bytes())
        seen = set()
        self.assertEqual(len(summary['sdk_wire_sha256']), 6)
        for label, digest in summary['sdk_wire_sha256'].items():
            raw = (self.path/(label+'.json')).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), digest)
            traces = verify(json.loads(raw), label, self.source)
            self.assertFalse(seen & traces)
            seen.update(traces)
        self.assertEqual(len(seen), 120)
        self.assertEqual(summary['source_sha256'], hashlib.sha256(self.source).hexdigest())

    def test_reject_corrupted_identity_and_content(self):
        mutations = [
            ('assistant-read', ('beta',), None),
            ('thread-read', ('trace',), self.data['requests'][0]['trace']),
            ('message-read', ('response','content',0,'text','value'), 'another receipt'),
            ('thread-update', ('sdk_value','metadata','revision'), 'wrong'),
            ('message-read', ('sdk_value','thread_id'), 'thread_wrong'),
            ('assistant-foreign', ('scope',), 'owner'),
            ('message-foreign', ('status',), 200),
            ('message-deleted', ('response','error','code'), 'server_error'),
            ('assistant-list', ('sdk_value','ids'), []),
            ('thread-delete', ('sdk_value','deleted'), False),
        ]
        for step, path, value in mutations:
            with self.subTest(step=step, path=path):
                actor = deepcopy(self.data)
                obj = next(row for row in actor['requests'] if row['step'] == step)
                for key in path[:-1]:
                    obj = obj[key]
                obj[path[-1]] = value
                with self.assertRaises(AssertionError):
                    verify(actor, self.label, self.source)

    def test_distinct_clients_and_original_files(self):
        summary = json.loads((self.path/'cpu.json').read_bytes())
        digests = summary['sdk_wire_sha256']
        actors = list(read_clients(self.path, tuple(digests), digests, self.source))
        self.assertEqual({label for label,_ in actors}, set(digests))
        for bad in [{k:v for k,v in digests.items() if k != self.label},
                    {**digests,self.label:'0'*64}]:
            with self.assertRaises(ValueError):
                list(read_clients(self.path, tuple(digests), bad, self.source))

    def test_reject_duplicate_query_parameter(self):
        actor = deepcopy(self.data)
        row = next(r for r in actor['requests'] if r['step'] == 'assistant-list')
        row['query'] += '&limit=20'
        with self.assertRaises(AssertionError):
            verify(actor, self.label, self.source)

    def test_reject_missing_exchange_and_wrong_sdk(self):
        for label, data in [(self.label, dict(self.data,requests=self.data['requests'][:-1])),
                            ('node-6.34.0', self.data)]:
            with self.assertRaises(AssertionError):
                verify(data, label, self.source)

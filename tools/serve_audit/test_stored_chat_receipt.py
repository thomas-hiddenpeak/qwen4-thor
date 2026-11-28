"""Reject mismatched cross-version stored Chat receipts using the real device exchange."""
import copy
import json
from pathlib import Path
import unittest

from .stored_chat_receipt import verify


class StoredChatReceiptTest(unittest.TestCase):
    def setUp(self):
        evidence = Path(__file__).resolve().parents[2] / 'docs/evidence'
        pairs = json.loads((evidence/'serve-stored-chat-sdk-20261128/requests.json').read_bytes())
        self.pair = copy.deepcopy(pairs[0])
        origin = json.loads((evidence/self.pair['source']).read_bytes())['publication']
        self.original = (json.dumps(origin, indent=2)+'\n').encode()

    def check(self):
        p = self.pair
        return verify(p['writer'], p['reader'], p['request'], p['metadata'], self.original)

    def test_original(self):
        self.assertEqual(len(self.check()), 10)

    def test_changed_cursor(self):
        request = self.pair['reader']['requests'][5]
        request['query'] = 'order=asc&limit=1&after=another-message'
        with self.assertRaisesRegex(ValueError, 'wire query'):
            self.check()

    def test_wrong_reader(self):
        self.pair['reader']['sdk'] = self.pair['writer']['sdk']
        with self.assertRaisesRegex(ValueError, 'different SDK'):
            self.check()

    def test_substituted_project_document(self):
        self.original = self.original.replace(b'580', b'581')
        with self.assertRaisesRegex(ValueError, 'publication missing'):
            self.check()

    def test_other_client_read(self):
        self.pair['reader']['forbidden']['status'] = 200
        with self.assertRaisesRegex(ValueError, 'native 404'):
            self.check()

    def test_lost_metadata_after_update(self):
        self.pair['reader']['saved']['metadata'].clear()
        with self.assertRaisesRegex(ValueError, 'metadata'):
            self.check()

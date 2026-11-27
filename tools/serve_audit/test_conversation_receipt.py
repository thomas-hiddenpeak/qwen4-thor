"""Check that SDK successes cannot be attributed to the wrong path or client."""
import copy
import json
from pathlib import Path
import unittest

from tools.serve_audit.conversation_receipt import verify


RECORD = (Path(__file__).resolve().parents[2] / 'docs/evidence' /
          'serve-conversation-sdk-business-20261128' / 'requests.json')


class ConversationReceiptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = json.loads(RECORD.read_text())

    def test_distinct_exchanges(self):
        seen = set()
        versions = {'python': {'2.8.1', '2.33.0', '3.42.1', '3.55.1'},
                    'node': {'6.34.0', '7.47.0'}}
        for row in self.rows:
            language, version = row['language'], row['version']
            versions[language].remove(version)
            answer = '223' if version in ('3.55.1', '7.47.0') else None
            traces = verify(row, language=language, version=version, expected_answer=answer)
            self.assertTrue(seen.isdisjoint(traces))
            seen.update(traces)
        self.assertFalse(any(versions.values()))
        self.assertEqual(len(seen), 74)

    def test_wrong_page_ownership_or_request_identity(self):
        for index, key, changed in [(5, 'query', 'limit=1&order=asc&after=wrong'),
                                    (7, 'status', 200), (1, 'trace', None),
                                    (0, 'status', 401), (2, 'path', '/v1/models')]:
            with self.subTest(index=index, key=key):
                row = copy.deepcopy(self.rows[0])
                row['requests'][index][key] = changed
                with self.assertRaises(ValueError):
                    verify(row, language=row['language'], version=row['version'],
                           expected_answer=row['answer'])

    def test_missing_request_or_reused_trace(self):
        row = copy.deepcopy(self.rows[0])
        row['requests'][1]['trace'] = row['requests'][0]['trace']
        with self.assertRaises(ValueError):
            verify(row, language=row['language'], version=row['version'], expected_answer=row['answer'])
        row = copy.deepcopy(self.rows[0])
        row['requests'].pop()
        with self.assertRaises(ValueError):
            verify(row, language=row['language'], version=row['version'], expected_answer=row['answer'])


if __name__ == '__main__':
    unittest.main()

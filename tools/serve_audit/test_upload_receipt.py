"""Rejection checks for the persisted client handoff."""
import copy
import json
from pathlib import Path
import unittest

from tools.serve_audit.upload_receipt import verify


class UploadReceiptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[2]/'docs/evidence/serve-upload-sdk-recovery-20261128'
        cls.source = (root/'project-release.json').read_bytes()
        cls.rows = json.loads((root/'requests.json').read_text())
        cls.summary = json.loads((root/'business.json').read_text())

        delivery = root.parent/'serve-native-soak-retention-20261128'/'delivery.json'
        cls.issued = json.loads(delivery.read_text())['publication']

    def check(self, *, rows=None, takeover=None, runtime=None, source=None):
        return verify(self.source if source is None else source, self.rows if rows is None else rows,
                      self.summary['takeover'] if takeover is None else takeover,
                      self.summary['runtime'] if runtime is None else runtime, self.summary['handoff'])

    def test_original(self):
        self.assertEqual(json.loads(self.source), self.issued)
        self.assertEqual(self.check()['response_answer'], str(json.loads(self.source)['version_revision']))

    def test_another_registered_package(self):
        choice = dict(self.summary['takeover'], payload_manifest_sha256=self.summary['handoff']['primary'])
        with self.assertRaisesRegex(ValueError, 'registered package'):
            self.check(takeover=choice)

    def test_missing_or_replayed_request(self):
        with self.assertRaisesRegex(ValueError, 'request accounting'):
            self.check(rows=self.rows[:-1])
        rows = copy.deepcopy(self.rows)
        rows[8]['wire']['trace'] = rows[3]['wire']['trace']
        with self.assertRaisesRegex(ValueError, 'request identities'):
            self.check(rows=rows)

    def test_repeating_completion_must_return_the_original_file(self):
        rows = copy.deepcopy(self.rows)
        rows[8]['data']['file']['id'] = rows[13]['data']['file']['id']
        with self.assertRaisesRegex(ValueError, 'replayed completion'):
            self.check(rows=rows)

    def test_peer_must_be_denied(self):
        rows = copy.deepcopy(self.rows)
        rows[15]['wire']['status'] = 200
        with self.assertRaisesRegex(ValueError, 'wire step 16'):
            self.check(rows=rows)

    def test_actual_download_includes_original_bytes(self):
        with self.assertRaisesRegex(ValueError, 'downloaded source'):
            self.check(source=self.source+b'\n')

    def test_extra_model_generation(self):
        with self.assertRaisesRegex(ValueError, 'managed cleanup'):
            self.check(runtime=dict(self.summary['runtime'], model_starts=3))


if __name__ == '__main__':
    unittest.main()

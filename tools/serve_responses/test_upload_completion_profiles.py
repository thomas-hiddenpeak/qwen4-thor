"""Check that an accepted concurrent pair contains two overlapping wire requests."""
import copy
import json
from pathlib import Path
import unittest

from tools.serve_responses import upload_completion_profiles as profile


class CompletionProfiles(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        evidence = Path(__file__).resolve().parents[2]/'docs/evidence/serve-upload-completion-race-20261128'
        cls.receipt = json.loads((evidence/'sdk.json').read_bytes())

    def test_observed_sdk(self):
        profile.check(self.receipt,'single')

    def test_serial_execution_is_rejected(self):
        data = copy.deepcopy(self.receipt)
        path = '/v1/uploads/'+data['pairs'][0]['upload']+'/complete'
        first, second = [r for r in data['requests'] if r['path']==path]
        second['start_ns'] = first['headers_ns']
        with self.assertRaisesRegex(RuntimeError, 'concurrent requests'):
            profile.check(data,'single')

    def test_second_file_rejected(self):
        data = copy.deepcopy(self.receipt)
        data['pairs'][0]['replies'][1]['file'] = data['pairs'][1]['file']
        with self.assertRaisesRegex(RuntimeError, 'published identity'):
            profile.check(data,'single')

    def test_conflict_reason_checked(self):
        data = copy.deepcopy(self.receipt)
        conflict = next(r for r in data['pairs'][1]['replies'] if r['status']==409)
        conflict['code'] = 'object_revision_conflict'
        with self.assertRaisesRegex(RuntimeError, 'concurrent requests'):
            profile.check(data,'single')


if __name__ == '__main__':
    unittest.main()

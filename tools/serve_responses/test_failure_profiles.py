"""Exercise scoped failure observations and cross-capacity replay checks."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.serve_responses import failure_profiles as profiles
from tools.serve_service import package


class FailureProfilesTest(unittest.TestCase):
    def setUp(self):
        self.first = dict(package.read(profiles.PROOF)['strict_python'], restored=None)
        self.following = deepcopy(self.first)
        earlier = self.first['live']
        wire = self.first['captures']
        self.following['live']['id'] = 'resp_' + 'b' * 32
        self.following['restored'] = dict(id=earlier['id'], sequence=earlier['sequence'], get=200, peer=404,
                                          resource_sha256=wire['live-get.json'],
                                          terminal_sha256=wire['live-decoded-terminal.json'])
        self.following['captures'].update({'restored-get.json':wire['live-get.json'],
                                          'restored-terminal.json':wire['live-decoded-terminal.json']})

    def test_historical_resource_and_cursor(self):
        profiles.check(self.first)
        for key,value in [('id',self.first['live']['id']),('cause','explicit_cancel'),('sequence',0),('peer',200)]:
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                profiles.check(dict(self.first, **{key:value}))

    def test_recovered_resource_and_terminal_must_both_match(self):
        profiles.check(self.following, self.first)
        for key in ('restored-get.json','restored-terminal.json','decoded-event.json','get.json','cancel.json'):
            changed = deepcopy(self.following)
            changed['captures'][key] = '0' * 64
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                profiles.check(changed, self.first)
        for key,value in [('sequence',0),('peer',200),('id',self.following['live']['id'])]:
            changed = deepcopy(self.following)
            changed['restored'][key] = value
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                profiles.check(changed, self.first)

    def test_fresh_request_cannot_reuse_an_old_failure(self):
        self.following['live']['id'] = self.first['live']['id']
        with self.assertRaises(RuntimeError):
            profiles.check(self.following, self.first)

    def test_fresh_cancellation_requires_accepted_control_and_valid_cursor(self):
        for key,value in [('control',409),('get',404),('cause','timeout'),('sequence',False),('sequence',1200)]:
            changed = deepcopy(self.first)
            changed['live'][key] = value
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                profiles.check(changed)

    def test_seed_creates_an_exclusive_private_record(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            rid = self.first['id']
            source = base / ('a' * 64 + '.response.' + rid + '.json')
            data = dict(response=dict(id=rid, status='failed', error=dict(
                code=self.first['cause'], message='stopped')), events=[dict(
                type='response.failed', sequence_number=self.first['sequence'])])
            source.write_text(json.dumps(dict(record=dict(data=data))))
            proof = base / 'proof.json'
            proof.write_text(json.dumps(dict(strict_python=dict(self.first, source_sha256=package.sha(source)))))
            store = base / 'private'
            with mock.patch.object(profiles, 'PROOF', proof):
                old_mask = os.umask(0o002)
                try:
                    params = profiles.seed(source, store)
                finally:
                    os.umask(old_mask)
                self.assertEqual(params['id'], rid)
                self.assertEqual((store/source.name).stat().st_mode & 0o777, 0o600)
                self.assertEqual((store/source.name).read_bytes(), source.read_bytes())
                with self.assertRaises(FileExistsError):
                    profiles.seed(source, store)

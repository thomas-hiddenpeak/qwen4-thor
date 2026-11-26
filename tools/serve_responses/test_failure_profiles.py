"""Exercise scoped failure observations and cross-capacity replay checks."""
from copy import deepcopy
import unittest

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

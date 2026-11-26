from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from tools.serve_recovery import profile_live_reuse as reuse
from tools.serve_responses import chatkit_actions_profiles as profiles
from tools.serve_responses import chatkit_interrupted_retry_profiles as retry


class InterruptedRetryHandoff(unittest.TestCase):
    def setUp(self):
        self.release = {'previous_revision': 552, 'publication': {
            'publication_revision': 554, 'desired': 'STOPPED',
            'primary': '1' * 64, 'fallback': '2' * 64}}
        self.patch = patch.object(retry.package, 'read', return_value=self.release)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        identifier = lambda prefix, digit: prefix + digit * 32
        revision, digest = retry.document(self.release)
        file = identifier('file_', '1')
        original = dict(response=identifier('resp_', '2'), status='cancelled',
                        file=file, record_sha256='3' * 64, published=False,
                        binding=dict(item_id=identifier('item_', '4'), path='/content/0',
                                     binding_sha256='5' * 64, snapshot=dict(
                                         id=identifier('input_snapshot_', '6'), sha256='7' * 64)))
        source = dict(workflow=identifier('agent_', '8'), thread=identifier('cthr_', '9'),
                      file=file, item=identifier('msg_', 'a'), user=identifier('item_', 'b'),
                      received='5', partial='554', document_sha256=digest, interrupted=original,
                      deleted=404)
        self.first = dict(sdk='7.47.0', phase='primary', source=source, revision=revision, chat='42')
        replacement = dict(deepcopy(original), response=identifier('resp_', 'c'),
                           status='completed', record_sha256='d' * 64, published=True)
        recovered = dict(ids=[source['user'], identifier('msg_', 'e')], peer=404, sibling=404,
                         deleted=404, original=dict(deepcopy(original), published=True),
                         replacement=replacement, text=revision + ' is the publication revision.')
        self.second = dict(deepcopy(self.first), phase='light', recovered=recovered)

    def test_unpublished_item_recovers_from_same_snapshot(self):
        retry.check(self.first)
        retry.check(self.second, self.first)
        spec = profiles.handoff({retry.KEY: {}})
        self.assertEqual((spec.actor, spec.receipt, spec.predecessor),
                         (retry.ACTOR, reuse.INTERRUPTED_RETRY_RECEIPT, reuse.GRID_RECEIPT))
        self.assertTrue(Path(spec.actor).is_file())

    def test_primary_must_cancel_before_item_publication_and_delete_source(self):
        for path, value in [(('source','interrupted','published'), True),
                            (('source','interrupted','status'), 'completed'),
                            (('source','partial'), 'other'), (('source','deleted'), 200),
                            (('source','document_sha256'), 'f' * 64)]:
            with self.subTest(path=path), self.assertRaises(RuntimeError):
                changed = deepcopy(self.first)
                obj = changed
                for key in path[:-1]:
                    obj = obj[key]
                obj[path[-1]] = value
                retry.check(changed)

    def test_restore_binds_the_original_journal_answer_and_browser_items(self):
        cases = [(('original','record_sha256'), '0' * 64),
                 (('replacement','binding','snapshot','id'), 'input_snapshot_' + '0' * 32),
                 (('replacement','file'), 'file_' + '0' * 32),
                 (('replacement','response'), self.second['recovered']['original']['response']),
                 (('replacement','published'), False),
                 (('ids',), [self.first['source']['user'], self.first['source']['item']]),
                 (('text',), '5541'), (('sibling',), 200), (('peer',), 200), (('deleted',), 200)]
        for path, value in cases:
            with self.subTest(path=path), self.assertRaises(RuntimeError):
                changed = deepcopy(self.second)
                obj = changed['recovered']
                for key in path[:-1]:
                    obj = obj[key]
                obj[path[-1]] = value
                retry.check(changed, self.first)

    def test_replacement_rejects_a_different_interrupted_thread(self):
        altered = deepcopy(self.second)
        altered['source']['thread'] = 'cthr_' + 'f' * 32
        with self.assertRaises(RuntimeError):
            retry.check(altered, self.first)

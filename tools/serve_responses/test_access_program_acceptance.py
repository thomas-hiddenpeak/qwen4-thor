"""Bounded driver oracles; all adapted responses here are CPU fixtures."""
import copy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from jsonschema import Draft202012Validator
from tools.serve_protocol.build_contract import chat_schema
from tools.serve_responses import encryption_acceptance as encrypted
from tools.serve_responses.access_program_acceptance import (
    LABEL, LIMITS, MAX_MODEL_STEPS, OTHER, Stage, check_selection, check_steps,
    foreground, negatives, policy, requests)
from tools.serve_responses.test_contract import errors
from tools.serve_service.package import read, save, sha


class AccessProgramAcceptance(unittest.TestCase):
    def test_frozen_requests_and_negative_cases(self):
        self.assertEqual(LIMITS, dict(starts=1, chats=13, controls=2000, seconds=900))
        self.assertEqual(MAX_MODEL_STEPS, 8)
        rows = requests()
        self.assertEqual(len(rows), 8)
        for name, row in rows.items():
            self.assertNotIn(LABEL, json.dumps(row))
            if name == 'chat':
                self.assertEqual(list(Draft202012Validator(chat_schema()).iter_errors(row)), [])
            else:
                self.assertEqual(errors(row, 'response_create'), [], name)
        self.assertTrue(rows['background']['background'])
        self.assertTrue(rows['explicit']['stream'])
        self.assertEqual(rows['tool']['tool_choice'], 'required')
        self.assertEqual(len(negatives()), 5)
        self.assertEqual([n[3] for n in negatives()], [403, 400, 400, 400, 403])
        self.assertEqual(policy()['minimum_grant'], 'red')
        self.assertEqual(policy()['clients'], [dict(id=encrypted.CLIENT, grant='red'),
                                              dict(id=OTHER, grant='blue')])

    def test_actual_wire_based_oracle_rejects_false_selection_and_context(self):
        files = list(Path(os.environ['Q4T_TEST_OUTPUT_DIR']).rglob('access-program.json.wire'))
        self.assertEqual(len(files), 1)
        value = read(files[0])
        value['model'] = foreground.MODEL
        value['q4t_access_program']['backend_model'] = foreground.MODEL
        digest = value['q4t_access_program']['policy_sha256']
        value['output'][0]['content'][0]['text'] = json.dumps(dict(label=LABEL))
        check_selection(value, digest, 'daybreak_blue', label=True)
        for mutation in ('selected', 'authority', 'digest', 'model', 'treatment', 'label'):
            wrong = copy.deepcopy(value)
            if mutation == 'selected':
                wrong['access_programs'] = None
            elif mutation == 'label':
                wrong['output'][0]['content'][0]['text'] = '{"label":"invented"}'
            else:
                key = dict(authority='authority', digest='policy_sha256',
                           model='backend_model', treatment='treatment')[mutation]
                wrong['q4t_access_program'][key] = 'unrelated'
            with self.assertRaises(RuntimeError):
                check_selection(wrong, digest, 'daybreak_blue', label=True)

    def test_model_steps_reject_denied_execution_skips_and_replays(self):
        values = {name: dict(id='response_'+name) for name in requests() if name != 'chat'}
        chat = dict(id='chat_actual')
        log = ''.join('[sampling] request='+v['id']+' choice=0/1 temperature=0\n'
                      for v in [*values.values(), chat])
        check_steps(log, values, chat)
        for wrong in (log+log.splitlines(keepends=True)[0],
                      '\n'.join(log.splitlines()[1:]),
                      log+'[sampling] request=denied choice=0/1 temperature=0\n'):
            with self.assertRaises(RuntimeError):
                check_steps(wrong, values, chat)

    def test_policy_copy_and_business_do_not_start_another_model(self):
        root = Path(os.environ['Q4T_TEST_OUTPUT_DIR'])
        out = Path(tempfile.mkdtemp(prefix='driver-', dir=root))
        obj = SimpleNamespace(out=out, plan=dict(access_program_policy=policy()),
                              counts=dict(starts=1))
        args = Stage.extra_model_args(obj)
        self.assertEqual(args, ['--access-program-policy-file', str(out/'access-program-policy.json')])
        self.assertEqual(obj.policy_digest, sha(args[1]))
        self.assertEqual(read(out/'access-program-policy-source.json'), policy())
        save(out/'capabilities-1.json', dict(responses=dict(access_programs=dict(
            policy_configured=True, policy_sha256=obj.policy_digest,
            authority='q4t-local', cloud_approval=False))))
        obj.plan['negative_requests'] = negatives()
        class FirstRequestReached(Exception):
            pass
        obj.sdk = lambda *a, **kw: (_ for _ in ()).throw(FirstRequestReached())
        with self.assertRaises(FirstRequestReached):
            Stage.business(obj)
        self.assertFalse((out/'access-program-policy.json').exists())
        self.assertEqual(obj.counts['starts'], 1)
        self.assertTrue((out/'access-program-policy-source.json').is_file())


if __name__ == '__main__':
    unittest.main()

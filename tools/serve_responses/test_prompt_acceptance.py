"""Failure-sensitive actual Prompt business oracles, no model startup."""
import copy
import json
import os
from pathlib import Path
import unittest

from tools.serve_responses.prompt_acceptance import (
    LIMITS, MAX_MODEL_STEPS, ROOT, check_delta_padding, check_value,
    definitions, requests)
from tools.serve_responses.test_contract import errors


def values():
    path = Path(os.environ.get('Q4T_PROMPT_WIRE', str(ROOT/
        'docs/evidence/serve-prompt-output-fix-20261110/fixtures/prompt-wire.jsonl')))
    return [json.loads(line) for line in path.read_text().splitlines()]


class PromptAcceptance(unittest.TestCase):
    def test_frozen_requests_and_definitions_use_sdk_shapes(self):
        self.assertEqual(LIMITS, dict(starts=2, chats=8, controls=2000, seconds=900))
        self.assertEqual(MAX_MODEL_STEPS, 7)
        for name, value in requests().items():
            if name in ('invalid', 'chat'):
                continue
            self.assertFalse(errors(value, 'response_create'), name)
        self.assertTrue(errors(requests()['invalid'], 'response_create'))
        for definition in definitions().values():
            self.assertFalse(errors(dict(definition=definition), 'prompt_create'))
        self.assertNotIn('42', json.dumps(requests()['first']))
        self.assertNotIn('73', json.dumps(requests()['second']))
        self.assertTrue(requests()['tool']['background'])
        self.assertEqual(requests()['explicit']['prompt']['version'], '1')

    def test_version_oracle_requires_actual_answer_identity_and_usage(self):
        value = next(v for v in values() if v.get('object') == 'response')
        identity = value['prompt']['id']
        check_value(value, identity, '1', 42)
        for change in ('version', 'identity', 'answer', 'usage'):
            bad = copy.deepcopy(value)
            if change == 'version':
                bad['prompt']['version'] = '2'
            elif change == 'identity':
                bad['prompt']['id'] = 'prompt_other'
            elif change == 'answer':
                next(item for item in bad['output'] if item['type'] == 'message')['content'][0]['text'] = '41'
            else:
                bad['usage']['input_tokens'] = 0
            with self.assertRaises(RuntimeError, msg=change):
                check_value(bad, identity, '1', 42)

    def test_padding_oracle_rejects_lifecycle_and_missing_delta_padding(self):
        chunks = [v for v in values() if 'sequence_number' in v]
        check_delta_padding(dict(chunks=chunks))
        for change in ('lifecycle', 'delta', 'absent'):
            bad = copy.deepcopy(chunks)
            if change == 'lifecycle':
                next(v for v in bad if not v['type'].endswith('.delta'))['obfuscation'] = 'x'
            elif change == 'delta':
                next(v for v in bad if v['type'].endswith('.delta')).pop('obfuscation')
            else:
                bad = []
            with self.assertRaises(RuntimeError, msg=change):
                check_delta_padding(dict(chunks=bad))


if __name__ == '__main__':
    unittest.main()

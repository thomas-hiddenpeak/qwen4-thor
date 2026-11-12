"""Bounded moderation business-driver oracles; adapted values are CPU fixtures."""
import base64
import copy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator
from tools.serve_protocol.build_contract import chat_schema
from tools.serve_responses import encryption_acceptance as encrypted
from tools.serve_responses.moderation_acceptance import (
    LIMITS, LIMITED_CLIENT, MAX_MODEL_STEPS, ROOT, STEPS, Stage, check_blocked_events,
    check_execution, check_review, foreground, requests)
from tools.serve_responses.test_contract import errors


def fixture():
    root = Path(os.environ['Q4T_TEST_OUTPUT_DIR'])
    value = json.loads((root / 'moderation-block.json.wire').read_text())
    for report in value['moderation'].values():
        report['model'] = foreground.MODEL
    return value


class ModerationAcceptance(unittest.TestCase):
    def test_fixed_distinct_requests_use_standard_shapes_and_budgets(self):
        self.assertEqual(LIMITS, dict(starts=1, chats=10, controls=2000, seconds=900))
        self.assertEqual(MAX_MODEL_STEPS, 28)
        rows = requests()
        self.assertEqual(set(rows), set(STEPS))
        for name, row in rows.items():
            if name == 'chat':
                self.assertEqual(list(Draft202012Validator(chat_schema()).iter_errors(row)), [])
            else:
                self.assertEqual(errors(row, 'response_create'), [], name)
                self.assertEqual(row['moderation']['model'], foreground.MODEL)
        self.assertTrue(rows['background']['background'])
        self.assertTrue(rows['prewarm']['prompt_cache_options']['prewarm'])
        self.assertEqual(rows['combined']['reasoning']['mode'], 'pro')
        self.assertEqual(rows['combined']['reasoning']['summary'], 'concise')
        self.assertEqual(rows['tool']['tool_choice'], 'required')

    def test_review_rejects_missing_fake_usage_wrong_model_and_partial_scores(self):
        value = fixture()
        check_review(value, blocked='output')
        for mutation in ('model', 'partial', 'usage', 'steps', 'threshold', 'leak'):
            bad = copy.deepcopy(value)
            if mutation == 'model':
                bad['moderation']['input']['model'] = 'omni-moderation-latest'
            elif mutation == 'partial':
                bad['moderation']['output']['category_scores'].pop('hate')
            elif mutation == 'usage':
                bad['q4t_moderation_usage']['total_tokens'] = 0
            elif mutation == 'steps':
                bad['q4t_moderation_usage']['model_steps'] = 0
            elif mutation == 'threshold':
                bad['moderation']['output']['category_scores']['violence'] = .1
            else:
                bad['output'] = [dict(type='message', id='msg_private', role='assistant',
                    status='incomplete', content=[dict(type='output_text', text='PRIVATE',
                                                    annotations=[], logprobs=[])])]
            with self.assertRaises(RuntimeError, msg=mutation):
                check_review(bad, blocked='output')

    def test_filtered_sse_rejects_private_delta_and_public_sequence_gap(self):
        value = fixture()
        events = [dict(type='response.created', sequence_number=0),
                  dict(type='response.incomplete', sequence_number=1, response=value)]
        check_blocked_events(value, events)
        for bad in ([dict(type='response.output_text.delta', sequence_number=0), events[-1]],
                    [events[0], dict(events[-1], sequence_number=2)]):
            with self.assertRaises(RuntimeError):
                check_blocked_events(value, bad)

    def test_actual_step_oracle_rejects_skipped_or_replayed_generation(self):
        values = {name: dict(id='resp_' + name) for name in STEPS if name != 'chat'}
        chat = dict(id='chat_ordinary')
        records = dict(values, chat=chat)
        log = ''.join('[sampling] request=' + records[name]['id'] +
            f' choice={i}/1 temperature=0\n' for name, count in STEPS.items()
            for i in range(count))
        check_execution(log, values, chat)
        lines = log.splitlines(keepends=True)
        for bad in (''.join(lines[1:]), log + lines[0], log.replace('choice=2/', 'choice=1/')):
            with self.assertRaises(RuntimeError):
                check_execution(bad, values, chat)

    def test_business_preserves_single_start_and_separate_limited_client(self):
        class FirstRequestReached(Exception):
            pass
        def sdk(label, action, request):
            self.assertEqual((label, action), ('score', 'create'))
            raise FirstRequestReached()
        with tempfile.TemporaryDirectory(dir=ROOT / '.q4t-work') as directory:
            root = Path(directory)
            (root / 'capabilities-1.json').write_text(json.dumps(dict(responses=dict(
                moderation=dict(classifier_available=True, block_output_before_publication=True,
                                same_client_tpm=True)))))
            stage = SimpleNamespace(out=root, counts=dict(starts=1), sdk=sdk,
                start=lambda: self.fail('duplicate model start'))
            with self.assertRaises(FirstRequestReached):
                Stage.business(stage)
        actual = object.__new__(Stage).client_policy()
        self.assertEqual(actual['clients'][0]['id'], encrypted.CLIENT)
        self.assertEqual(actual['clients'][0]['max_prompt_tokens'], 16384)
        self.assertEqual(actual['clients'][1]['id'], LIMITED_CLIENT)
        self.assertEqual(actual['clients'][1]['max_prompt_tokens'], 4096)

    def test_sdk_honors_explicit_client_and_can_observe_active_background(self):
        reply = dict(ok=True, result=fixture(), chunks=[], exchanges=[{}],
                     wire_base64=base64.b64encode(b'{}').decode())
        received = []
        def execute(argv, **kwargs):
            received.append(json.loads(kwargs['input']))
            return SimpleNamespace(returncode=0, stderr='', stdout=json.dumps(reply))
        with tempfile.TemporaryDirectory(dir=ROOT / '.q4t-work') as directory:
            stage = SimpleNamespace(out=Path(directory),
                plan=dict(port=18993, sdk=dict(sdk_directory='cpu-only')),
                budget=lambda kind: self.assertEqual(kind, 'chats'),
                same_model=lambda: None,
                drained=lambda label: self.fail('background was drained before observation'))
            with patch.object(encrypted.subprocess, 'run', side_effect=execute):
                encrypted.Stage.sdk(stage, 'cpu', 'create', requests()['score'],
                                    headers={'X-Q4T-Client-Id': LIMITED_CLIENT}, drain=False)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]['headers']['X-Q4T-Client-Id'], LIMITED_CLIENT)


if __name__ == '__main__':
    unittest.main()

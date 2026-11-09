"""Fixed pro business oracles; adapted values are CPU fixtures only."""
import copy
import json
from pathlib import Path
import unittest
import tempfile
from types import SimpleNamespace

from jsonschema import Draft202012Validator
from tools.serve_protocol.build_contract import chat_schema
from tools.serve_responses.reasoning_acceptance import (
    LIMITS, MAX_MODEL_STEPS, ROOT, Stage, check_execution, check_pro, requests)
from tools.serve_responses.test_contract import errors


def response():
    return json.loads((ROOT / 'docs/evidence/serve-reasoning-pro-cpu-20261110/'
        'cpu-01/pro.json.wire').read_text())


class ReasoningAcceptance(unittest.TestCase):
    def test_frozen_requests_cover_distinct_real_execution_paths(self):
        self.assertEqual(LIMITS, dict(starts=2, chats=9, controls=2000, seconds=900))
        self.assertEqual(MAX_MODEL_STEPS, 17)
        rows = requests()
        self.assertEqual(len(rows), 9)
        for name, value in rows.items():
            if name == 'chat':
                self.assertEqual(list(Draft202012Validator(chat_schema()).iter_errors(value)), [])
            else:
                self.assertEqual(errors(value, 'response_create'), [], name)
                self.assertEqual(value['reasoning']['mode'], 'pro')
        self.assertTrue(rows['prewarm']['prompt_cache_options']['prewarm'])
        self.assertEqual(rows['tiny']['max_output_tokens'], 1)
        self.assertEqual(rows['summary']['reasoning']['summary'], 'concise')
        self.assertTrue(rows['background']['background'])
        self.assertEqual(rows['tool']['tool_choice'], 'required')
        self.assertEqual(rows['follow']['tool_choice'], 'none')
        self.assertEqual(rows['compacted']['context_management'][0]['type'], 'compaction')

    def test_business_uses_already_started_model_without_second_start(self):
        class FirstRequestReached(Exception):
            pass
        def sdk(label, action, request):
            self.assertEqual((label, action), ('structured', 'create'))
            raise FirstRequestReached()
        with tempfile.TemporaryDirectory(dir=ROOT / '.q4t-work') as directory:
            root = Path(directory)
            (root / 'capabilities-2.json').write_text(json.dumps(dict(responses=dict(
                reasoning_execution=dict(pro='native_deliberation_then_answer',
                    shared_output_budget=True, speculative_tool_execution=False)))))
            stage = SimpleNamespace(out=root, counts=dict(starts=2), sdk=sdk,
                start=lambda: self.fail('business attempted an extra model start'))
            with self.assertRaises(FirstRequestReached):
                Stage.business(stage)

    def test_final_requires_actual_notes_mode_usage_and_current_answer(self):
        value = response()
        check_pro(value, answer='42')
        for change in ('notes', 'usage', 'mode', 'answer', 'budget'):
            bad = copy.deepcopy(value)
            if change == 'notes':
                bad['output'] = [item for item in bad['output'] if item['type'] != 'reasoning']
            elif change == 'usage':
                bad['usage']['output_tokens_details']['reasoning_tokens'] = 0
            elif change == 'mode':
                bad['reasoning']['mode'] = 'standard'
            elif change == 'answer':
                next(item for item in bad['output'] if item['type'] == 'message')['content'][0]['text'] = '41'
            else:
                bad['max_output_tokens'] = 1
            with self.assertRaises(RuntimeError, msg=change):
                check_pro(bad, answer='42')

    def test_tiny_cannot_refill_or_fabricate_a_final_answer(self):
        value = response()
        value.update(status='incomplete', incomplete_details=dict(reason='max_output_tokens'),
                     max_output_tokens=1)
        value['output'] = [item for item in value['output'] if item['type'] == 'reasoning']
        value['usage'].update(output_tokens=1, total_tokens=value['usage']['input_tokens'] + 1,
                              output_tokens_details=dict(reasoning_tokens=1))
        check_pro(value, incomplete=True)
        for change in ('refill', 'final', 'reason'):
            bad = copy.deepcopy(value)
            if change == 'refill':
                bad['max_output_tokens'] = 2
                bad['usage']['output_tokens'] = 2
                bad['usage']['total_tokens'] += 1
            elif change == 'final':
                bad['output'].append(response()['output'][-1])
            else:
                bad['incomplete_details']['reason'] = 'content_filter'
            with self.assertRaises(RuntimeError, msg=change):
                check_pro(bad, incomplete=True)

    def test_execution_receipts_reject_skipped_extra_and_replayed_steps(self):
        counts = dict(structured=2, summary=3, background=2, tool=2,
                      follow=2, tiny=1, prewarm=1, compacted=3, chat=1)
        values = {name: dict(id='resp_' + name) for name in counts}
        log = ''.join('[sampling] request=' + values[name]['id'] +
            f' choice={index}/1 temperature=0\n' for name, count in counts.items()
            for index in range(count))
        check_execution(log, values, values['chat'])
        lines = log.splitlines(keepends=True)
        for bad in (''.join(lines[1:]), log + lines[0], log.replace('choice=2/', 'choice=1/')):
            with self.assertRaises(RuntimeError):
                check_execution(bad, values, values['chat'])


if __name__ == '__main__':
    unittest.main()

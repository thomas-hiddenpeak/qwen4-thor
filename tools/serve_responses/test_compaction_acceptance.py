"""Fail-sensitive compaction acceptance oracles; CPU only, no model spawn."""
import copy
import json
import unittest

from tools.serve_responses import encryption_acceptance as encrypted
from tools.serve_responses.compaction_acceptance import (
    LIMITS, MAX_MODEL_STEPS, ROOT, Stage, check_reduction, compact_item, requests)
from tools.serve_responses.test_contract import errors

EVIDENCE = ROOT/'docs/evidence/serve-compaction-execution-20261109'


def automatic():
    return json.loads((EVIDENCE/'fixtures-03/compaction-wire/automatic-result.wire').read_text())


def standalone():
    return json.loads((EVIDENCE/'fixtures/compaction-wire/standalone-result.wire').read_text())


class CompactionAcceptance(unittest.TestCase):
    def test_frozen_requests_have_actual_routes_and_bounded_steps(self):
        rows = requests()
        self.assertEqual(LIMITS, dict(starts=2, chats=8, controls=2000, seconds=900))
        self.assertEqual(len(rows), LIMITS['chats'])
        self.assertEqual(MAX_MODEL_STEPS, 9)
        for name, row in rows.items():
            if name == 'chat':
                continue
            kind = 'response_compact' if name in ('compact', 'noop') else 'response_create'
            self.assertFalse(errors(row, kind), name)
        self.assertTrue(rows['automatic']['stream'])
        self.assertNotIn('stream_options', rows['automatic'])
        self.assertEqual(rows['automatic']['reasoning']['summary'], 'concise')
        self.assertEqual(rows['background_tool']['input'][-1], dict(type='compaction_trigger'))
        self.assertTrue(rows['background_tool']['background'])
        self.assertNotIn('42', rows['resumed'].get('instructions', ''))

    def test_compact_oracle_rejects_missing_fake_or_mismatched_state(self):
        for value in (automatic(), standalone()):
            self.assertTrue(compact_item(value)['encrypted_content'].startswith('q4tc1:'))
        for change in ('missing', 'duplicate', 'plain', 'wrong_id', 'status'):
            value = standalone()
            item = value['output'][-1]
            if change == 'missing':
                value['output'].pop()
            elif change == 'duplicate':
                value['output'].append(copy.deepcopy(item))
            elif change == 'plain':
                item['encrypted_content'] = 'The answer is 42.'
            elif change == 'wrong_id':
                item['id'] = 'cmp_wrong'
            else:
                item['status'] = 'completed'
            with self.assertRaises(RuntimeError, msg=change):
                compact_item(value)

    def test_reduction_requires_real_count_and_preserved_fact(self):
        value = automatic()
        original = value['usage']['input_tokens'] + 100
        result = check_reduction(dict(prompt_tokens=original), value)
        self.assertEqual(result['saved_tokens'], 100)
        for change in ('equal', 'larger', 'zero', 'wrong_answer'):
            bad = copy.deepcopy(value)
            if change == 'wrong_answer':
                next(v for v in bad['output'] if v['type'] == 'message')['content'][0]['text'] = '41'
            else:
                bad['usage']['input_tokens'] = dict(equal=original, larger=original+1, zero=0)[change]
            with self.assertRaises(RuntimeError, msg=change):
                check_reduction(dict(prompt_tokens=original), bad)

    def test_noop_admission_and_failure_cannot_hide_usage_or_extra_calls(self):
        value = standalone()
        noop = copy.deepcopy(value)
        for key in ('input_tokens', 'output_tokens', 'total_tokens'):
            noop['usage'][key] = 0
        key = '{client="'+encrypted.CLIENT+'"}'
        usage = value['usage']
        metrics = dict(q4t_requests_total=3, q4t_requests_success_total=2,
            q4t_requests_error_total=1, q4t_prompt_tokens_total=usage['input_tokens'],
            q4t_generation_tokens_total=usage['output_tokens'])
        metrics.update({'q4t_client_rate_admitted_total'+key: 2,
            'q4t_client_rate_reserved_tokens'+key: 0,
            'q4t_client_rate_settled_tokens_total'+key: usage['total_tokens']})
        stage = Stage.__new__(Stage)
        stage.drained = lambda _: metrics
        stage.accounting('fixture', [value, noop], rejected=1)
        for field in metrics:
            metrics[field] += 1
            with self.assertRaises(RuntimeError, msg=field):
                stage.accounting('mutant', [value, noop], rejected=1)
            metrics[field] -= 1


if __name__ == '__main__':
    unittest.main()

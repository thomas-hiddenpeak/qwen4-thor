"""Negative continuation verifier fixtures, never evidence of actual generation."""
import copy
import json
import unittest

from tools.serve_realtime import prompt_followup as p
from tools.serve_realtime.test_prompt_acceptance import fixture, replace_row


def failed_prefix():
    value, record = fixture()
    value.update(status='FAIL', response_creates=8, http_calls=7)
    value['responses'] = value['responses'][:8]
    value['events'] = [e for e in value['events'] if e.get('response_id') not in ('resp_8', 'resp_9')
        and e.get('response', {}).get('id') not in ('resp_8', 'resp_9')]
    call = value['responses'][6]['output'][0]
    call['call_id'] = 'actual-call'
    replace_row(value, 6, lambda r: None)
    answer = 'No version key exists in the tool result.'
    replace_row(value, 7, lambda r: r['output'][0]['content'][0].update(text=answer))
    next(e for e in value['events'] if e['type'] == 'response.output_text.delta' and
         e['response_id'] == 'resp_7')['delta'] = answer
    value['error'] = 'actual tool round trip quality: ' + json.dumps(answer)
    value['events'][-2:-2] = [dict(type='conversation.item.done', item=dict(
        type='function_call_output', call_id=call['call_id'], output=json.dumps(value['tool_record']))),
        dict(type='response.created', response=dict(id='resp_7'))]
    return value, record


def remaining():
    value, _ = fixture()
    value.update(mode='remaining', response_creates=2, http_calls=0)
    value['responses'] = value['responses'][8:]
    value['events'] = [e for e in value['events'] if e.get('response_id') in ('resp_8', 'resp_9')
        or e.get('response', {}).get('id') in ('resp_8', 'resp_9')]
    value['events'][:0] = [dict(type='session.updated', session=dict(
        prompt=dict(id='owned-prompt', version='1'))) for _ in range(2)]
    return value


class ContinuationEvidence(unittest.TestCase):
    def test_real_prefix_cannot_fill_missing_cases_or_reclassify_failure(self):
        value, record = failed_prefix()
        self.assertEqual(p.check_prior(value, record, 52), 112)
        self.assertEqual(len(value['responses']), 8)
        self.assertEqual(value['status'], 'FAIL')
        for field, new in [('status', 'PASS'), ('response_creates', 10),
                           ('connections', 2), ('automatic_replay', True)]:
            changed = copy.deepcopy(value); changed[field] = new
            with self.assertRaises(Exception): p.check_prior(changed, record, 52)

    def test_missing_tool_result_wrong_source_or_stream_rejects_reuse(self):
        value, record = failed_prefix()
        for mutate in [lambda v: v['tool_record'].update(version='invented'),
                       lambda v: v['events'].__setitem__(slice(None), [e for e in v['events'] if
                           e['type'] != 'conversation.item.done']),
                       lambda v: v['events'].append(copy.deepcopy(v['events'][-1])),
                       lambda v: v.update(error='accepted model failure'),
                       lambda v: replace_row(v, 3, lambda r: r['usage'].update(total_tokens=999))]:
            changed = copy.deepcopy(value); mutate(changed)
            with self.assertRaises(Exception): p.check_prior(changed, record, 52)

    def test_remaining_exact_terminals_usage_and_no_replay(self):
        value = remaining()
        self.assertEqual(p.check_remaining(value, 'owned-prompt'), 28)
        for mutate in [lambda v: v.update(response_creates=3),
                       lambda v: v.update(http_calls=1),
                       lambda v: v['events'].append(copy.deepcopy(v['events'][-1])),
                       lambda v: replace_row(v, 0, lambda r: r.update(status='completed')),
                       lambda v: replace_row(v, 1, lambda r: r.update(conversation_id='leaked'))]:
            changed = copy.deepcopy(value); mutate(changed)
            with self.assertRaises(Exception): p.check_remaining(changed, 'owned-prompt')

    def test_actual_delta_and_pinned_template_remain_required(self):
        value = remaining()
        for mutate in [lambda v: v['events'].__setitem__(slice(None), [e for e in v['events'] if
                           not (e['type'] == 'response.output_text.delta' and e['response_id'] == 'resp_8')]),
                       lambda v: v['events'][0]['session']['prompt'].update(version='2'),
                       lambda v: replace_row(v, 1, lambda r: r['output'][0]['content'][0].update(text='wrong'))]:
            changed = copy.deepcopy(value); mutate(changed)
            with self.assertRaises(Exception): p.check_remaining(changed, 'owned-prompt')

    def test_diagnostics_change_only_frozen_sampling_fields(self):
        request = dict(temperature=1, top_p=1, stream=False,
                       messages=[dict(role='tool', content='exact source')])
        before = copy.deepcopy(request)
        for i, temperature in enumerate((1, 0)):
            value = p.diagnostic_request(request, i)
            self.assertEqual(value, dict(before, temperature=temperature, seed=0))
        self.assertEqual(request, before)
        for index in (-1, 2, True):
            with self.assertRaises(Exception): p.diagnostic_request(request, index)

    def test_quality_fail_does_not_hide_protocol_failure_or_expand_scope(self):
        row = dict(choices=[dict(finish_reason='stop', message=dict(content='wrong'))],
                   usage=dict(prompt_tokens=305, completion_tokens=2, total_tokens=307))
        self.assertEqual(p.check_diagnostic(row, 'rev356', 305)['quality'], 'FAIL')
        for mutate in [lambda v: v['choices'][0].update(finish_reason='length'),
                       lambda v: v['usage'].update(prompt_tokens=304, total_tokens=306),
                       lambda v: v['choices'][0]['message'].update(content=None),
                       lambda v: v['usage'].update(total_tokens=999)]:
            changed = copy.deepcopy(row); mutate(changed)
            with self.assertRaises(Exception): p.check_diagnostic(changed, 'rev356', 305)
        plan = dict(schema='q4t-realtime-prompt-followup-v1', limits=p.LIMITS,
                    contract=json.loads(json.dumps(p.CONTRACT)))
        p.check_plan(plan)
        for mutate in [lambda v: v['limits'].update(starts=2),
                       lambda v: v['contract'].update(original_quality='PASS'),
                       lambda v: v['contract']['fixed_diagnostics'].append(['retry', 0, 1])]:
            changed = copy.deepcopy(plan); mutate(changed)
            with self.assertRaises(Exception): p.check_plan(changed)


if __name__ == '__main__': unittest.main()

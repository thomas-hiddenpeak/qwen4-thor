"""CPU receipts are negative verifier fixtures, never actual model evidence."""
import copy
import unittest

from tools.serve_realtime import prompt_acceptance as p


def fixture(mode='alpha'):
    record = dict(path=str(p.ROOT / 'docs/STATUS.md'), version='rev356', sha256='a' * 64)
    answers = ['42', '377', '72', 'rev356', 'red', '52', '', 'rev356', '1', '56'] \
        if mode == 'alpha' else ['', '52', '']
    rows, events = [], []
    for index, (case, answer) in enumerate(zip(p.CASES[mode], answers)):
        status = ('cancelled' if index == 8 else 'completed') if mode == 'alpha' \
            else ('completed' if index == 1 else 'failed')
        denied = status == 'failed'
        row = dict(id=f'resp_{index}', status=status,
            metadata=dict(acceptance_case=case), output=[],
            usage=dict(input_tokens=0 if denied else 12,
                       output_tokens=0 if denied else 2,
                       total_tokens=0 if denied else 14))
        if denied:
            row['status_details'] = dict(error=dict(code='object_not_found' if index == 0 else 'client_rpm_exhausted'))
        elif mode == 'alpha' and index == 6:
            row['output'] = [dict(id='item_tool', type='function_call', status='completed',
                name='lookup_project_record', arguments='{"record_key":"release"}')]
            events.append(dict(type='response.function_call_arguments.delta', response_id=row['id'],
                item_id='item_tool', delta=row['output'][0]['arguments']))
        else:
            row['output'] = [dict(type='message', content=[dict(type='output_text', text=answer)])]
            events.append(dict(type='response.output_text.delta', response_id=row['id'], delta=answer))
        rows.append(row); events.append(dict(type='response.done', response=copy.deepcopy(row)))
    source = dict(version=record['version'], source='docs/STATUS.md', source_sha256=record['sha256'])
    if mode == 'alpha':
        events[:0] = [dict(type='session.updated', session=dict(prompt=dict(version='1'), max_output_tokens=128)) for _ in range(4)]
    else:
        events.insert(0, dict(type='error', error=dict(event_id='foreign-prompt-denied', code='object_not_found')))
    result = dict(status='PASS', mode=mode, transport='official-openai-wss', connections=1,
        close_code=1000, automatic_replay=False, response_creates=len(rows), http_calls=7,
        responses=rows, events=events, source_record=source,
        tool_record=dict(record_key='release', **source))
    return result, record


def replace_row(value, index, change):
    row = value['responses'][index]; change(row)
    for event in value['events']:
        if event['type'] == 'response.done' and event['response']['id'] == row['id']:
            event['response'] = copy.deepcopy(row)


class PromptEvidence(unittest.TestCase):
    def test_positive_cpu_receipts_and_exact_cumulative_usage(self):
        for mode, total in [('alpha', 140), ('beta', 14)]:
            value, record = fixture(mode)
            self.assertEqual(p.check_receipt(value, mode, record, 52), total)

    def test_no_replay_scope_expansion_or_changed_terminal(self):
        value, record = fixture()
        for field, new in [('connections', 2), ('automatic_replay', True),
                           ('response_creates', 11), ('http_calls', 17)]:
            changed = copy.deepcopy(value); changed[field] = new
            with self.assertRaises(Exception): p.check_receipt(changed, 'alpha', record, 52)
        for mutate in [lambda v: v['events'].append(v['events'][-1]),
                       lambda v: v['events'].pop(),
                       lambda v: v['responses'][0]['metadata'].update(acceptance_case='invented')]:
            changed = copy.deepcopy(value); mutate(changed)
            with self.assertRaises(Exception): p.check_receipt(changed, 'alpha', record, 52)

    def test_wrong_quality_source_or_missing_actual_cancel_rejected(self):
        value, record = fixture()
        for mutate in [lambda v: v['source_record'].update(source_sha256='b' * 64),
                       lambda v: v['tool_record'].update(version='rev0'),
                       lambda v: v['events'].__setitem__(slice(None), [e for e in v['events'] if
                           not (e['type'] == 'response.output_text.delta' and e['response_id'] == 'resp_8')])]:
            changed = copy.deepcopy(value); mutate(changed)
            with self.assertRaises(Exception): p.check_receipt(changed, 'alpha', record, 52)
        with self.assertRaises(Exception): p.check_receipt(value, 'alpha', record, 53)

    def test_stream_pinning_history_and_tool_arguments_cannot_be_fabricated(self):
        value, record = fixture()
        for mutate in [lambda v: v['events'][0]['session']['prompt'].update(version='2'),
                       lambda v: v['events'][1]['session'].update(max_output_tokens=4096),
                       lambda v: replace_row(v, 0, lambda r: r.update(conversation_id='leaked-prefix')),
                       lambda v: next(e for e in v['events'] if e['type'] ==
                           'response.function_call_arguments.delta').update(delta='{}'),
                       lambda v: next(e for e in v['events'] if e['type'] ==
                           'response.output_text.delta').update(delta='wrong')]:
            changed = copy.deepcopy(value); mutate(changed)
            with self.assertRaises(Exception): p.check_receipt(changed, 'alpha', record, 52)

    def test_denied_owners_cannot_consume_quota_or_claim_model_work(self):
        value, record = fixture('beta')
        for mutate in [lambda v: replace_row(v, 0, lambda r: r['usage'].update(
                           input_tokens=1, total_tokens=1)),
                       lambda v: replace_row(v, 2, lambda r: r['status_details']['error'].update(code='unknown')),
                       lambda v: v['events'][0]['error'].update(code='prompt_missing'),
                       lambda v: replace_row(v, 1, lambda r: r['usage'].update(output_tokens=True))]:
            changed = copy.deepcopy(value); mutate(changed)
            with self.assertRaises(Exception): p.check_receipt(changed, 'beta', record, 52)

    def test_frozen_scope_and_real_source_derivation_required(self):
        _, record = fixture()
        plan = dict(schema='q4t-realtime-prompt-business-v1', limits=dict(p.LIMITS),
            contract=dict(p.CONTRACT), record=record, csv_sum=52,
            csv_sources=[dict(tests=22), dict(tests=30)], image=dict(
                path=str(p.ROOT / 'tests/fixtures/file_images/original.png'), expected_left_color='red'))
        p.check_plan(plan)
        for mutate in [lambda v: v['limits'].update(starts=2),
                       lambda v: v['contract'].update(automatic_replay=True),
                       lambda v: v.update(csv_sum=53),
                       lambda v: v['csv_sources'][0].update(tests=True),
                       lambda v: v['image'].update(path='/tmp/unreviewed.png')]:
            changed = copy.deepcopy(plan); mutate(changed)
            with self.assertRaises(Exception): p.check_plan(changed)


if __name__ == '__main__': unittest.main()

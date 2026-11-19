"""Negative receipts must not certify actual Realtime execution or settlement."""
import copy
import unittest

from tools.serve_realtime import conversation_acceptance as c


def fixture(mode='main'):
    record = dict(path=str(c.ROOT / 'docs/STATUS.md'), version='rev346',
                  sha256='a' * 64)
    states = ['completed', 'failed'] if mode == 'quota' else [
        'completed', 'completed', 'completed', 'completed', 'completed',
        'incomplete', 'cancelled',
        'completed']
    rows = [dict(id=f'resp_{i}', status=s, output=[], usage=dict(
        input_tokens=12, output_tokens=1, total_tokens=13))
        for i, s in enumerate(states)]
    if mode == 'main':
        rows[5]['status_details'] = dict(reason='max_output_tokens')
    else:
        rows[1]['status_details'] = dict(error=dict(code='client_rpm_exhausted'))
        rows[1].pop('usage')
    events = [dict(type='response.done', response=copy.deepcopy(r)) for r in rows]
    if mode == 'main':
        events.insert(6, dict(type='response.output_text.delta',
                             response_id=rows[6]['id'], delta='1'))
    value = dict(status='PASS', connections=1, close_code=1000,
        automatic_replay=False, transport='official-openai-wss', mode=mode,
        response_creates=len(rows), responses=rows, events=events,
        tool_record=dict(record_key='release', version=record['version'],
                         source='docs/STATUS.md', source_sha256=record['sha256']))
    return value, record


class Evidence(unittest.TestCase):
    def test_frozen_scope_and_actual_record(self):
        _, record = fixture()
        plan = dict(schema='q4t-realtime-conversation-business-v1',
                    limits=dict(c.LIMITS), contract=dict(c.CONTRACT), record=record)
        c.check_plan(plan)
        for key in ('limits', 'contract', 'record'):
            altered = copy.deepcopy(plan)
            if key == 'limits': altered[key]['starts'] += 1
            elif key == 'contract': altered[key]['automatic_replay'] = True
            else: altered[key]['path'] = '/tmp/invented-release.txt'
            with self.assertRaises(Exception): c.check_plan(altered)

    def test_real_outcomes_and_project_provenance_cannot_be_claimed(self):
        value, record = fixture()
        self.assertEqual(c.check_receipt(value, 'main', record), 104)
        for field, replacement in [('connections', 2), ('close_code', 1006),
                ('automatic_replay', True), ('response_creates', 7)]:
            changed = copy.deepcopy(value); changed[field] = replacement
            with self.assertRaises(Exception): c.check_receipt(changed, 'main', record)
        changed = copy.deepcopy(value); changed['tool_record']['version'] = 'rev0'
        with self.assertRaises(Exception): c.check_receipt(changed, 'main', record)

    def test_repeated_or_different_terminal_and_no_actual_cancel_delta(self):
        value, record = fixture()
        for events in (value['events'] + [value['events'][-1]],
                       [e for e in value['events'] if e['type'] == 'response.done']):
            changed = copy.deepcopy(value); changed['events'] = events
            with self.assertRaises(Exception): c.check_receipt(changed, 'main', record)
        changed = copy.deepcopy(value)
        changed['events'][0]['response']['status'] = 'failed'
        with self.assertRaises(Exception): c.check_receipt(changed, 'main', record)

    def test_out_of_band_pollution_and_false_usage_rejected(self):
        value, record = fixture()
        changed = copy.deepcopy(value)
        changed['responses'][2]['conversation_id'] = 'conv_default'
        changed['events'][2]['response'] = changed['responses'][2]
        with self.assertRaises(Exception): c.check_receipt(changed, 'main', record)
        for usage in ({}, dict(input_tokens=True, output_tokens=1, total_tokens=2),
                      dict(input_tokens=12, output_tokens=1, total_tokens=12)):
            with self.assertRaises(Exception): c.response_usage(dict(usage=usage))

    def test_quota_refusal_and_exact_settlement_required(self):
        value, record = fixture('quota')
        self.assertEqual(c.check_receipt(value, 'quota', record), 13)
        value['responses'][1]['status_details']['error']['code'] = 'unknown'
        value['events'][1]['response'] = value['responses'][1]
        with self.assertRaises(Exception): c.check_receipt(value, 'quota', record)
        metrics = {key + '{client="alpha"}': count for key, count in (
            ('q4t_client_rate_admitted_total', 8),
            ('q4t_client_rate_settled_tokens_total', 104),
            ('q4t_client_rate_reserved_tokens', 0))}
        c.settlement(metrics, 'alpha', 104, 8)
        for key in metrics:
            changed = dict(metrics); changed[key] += 1
            with self.assertRaises(Exception): c.settlement(changed, 'alpha', 104, 8)


if __name__ == '__main__': unittest.main()

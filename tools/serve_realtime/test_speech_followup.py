"""New-user evidence cannot replace the immutable original quality failure."""
import copy
import unittest

from tools.serve_realtime import speech_followup as f
from tools.serve_realtime.test_speech_acceptance import fixture as original


def fixture():
    value, record = original()
    value.update(mode='content', response_creates=5)
    value['responses'] = value['responses'][:5]
    value['retrievals'] = [r for r in value['retrievals'] if
                           r['label'] in ('tool_audio', 'pcm', 'mulaw', 'alaw')]
    value['checks'] = [c for c in value['checks'] if c['type'] == 'speed']
    value['quality'] = value['quality'][:4]
    events = []
    for row in value['responses']:
        label, response = row['label'], row['response']
        if label != 'tool':
            events.append(dict(type='conversation.item.done', item=dict(
                id='user_tool_answer' if label == 'tool_audio' else 'user_' + label,
                type='message', role='user', content=[dict(type='input_text',
                    text=f.TOOL_TASK if label == 'tool_audio' else f.TASK)])))
        events.append(dict(type='response.created', response=dict(id=response['id'])))
        events += [e for e in value['events'] if e.get('response_id') == response['id'] or
                   (e['type'] == 'response.done' and e['response']['id'] == response['id'])]
    for i, event in enumerate(events): event['event_id'] = f'new_{i}'
    value['events'] = events
    return value, record


def observed():
    return [dict(label=a + '-' + b, native_prompt=a, measured_prompt=a,
                 executed_prompt=a, generation_request={'messages': []})
            for a in ('tool_audio', 'pcm') for b in ('audio', 'text', 'new_user')]


class FollowupEvidence(unittest.TestCase):
    def test_actual_new_turns_and_audio_have_exact_usage(self):
        value, record = fixture()
        total, audio = f.check_receipt(value, record)
        self.assertEqual(total, 1005)
        self.assertEqual(list(audio), ['tool_audio', 'pcm', 'mulaw', 'alaw'])

    def test_missing_or_late_user_turn_does_not_certify_content(self):
        value, record = fixture()
        for operation in ('missing', 'late'):
            wrong = copy.deepcopy(value)
            i = next(i for i,e in enumerate(wrong['events']) if
                     e.get('item', {}).get('id') == 'user_pcm')
            event = wrong['events'].pop(i)
            if operation == 'late': wrong['events'].append(event)
            with self.assertRaises(Exception): f.check_receipt(wrong, record)

    def test_changed_task_or_role_does_not_certify_new_user(self):
        value, record = fixture()
        for field, replacement in [('role', 'assistant'), ('content', [])]:
            wrong = copy.deepcopy(value)
            next(e['item'] for e in wrong['events'] if
                 e.get('item', {}).get('id') == 'user_pcm')[field] = replacement
            with self.assertRaises(Exception): f.check_receipt(wrong, record)

    def test_wrong_quality_replay_and_partial_formats_do_not_pass(self):
        value, record = fixture()
        for change in [lambda v:v.update(automatic_replay=True),
                       lambda v:v['quality'][0].update(actual='wrong'),
                       lambda v:v['retrievals'].pop(),
                       lambda v:v['events'].append(v['events'][-1])]:
            wrong = copy.deepcopy(value); change(wrong)
            with self.assertRaises(Exception): f.check_receipt(wrong, record)

    def test_fixed_seed_comparison_uses_exact_original_prompts(self):
        rows = observed()
        requests = f.comparison_requests(rows)
        self.assertEqual(requests['pcm'], dict(model='qwen3.8-flash-next',
            prompt='pcm', max_tokens=64, temperature=0, seed=0))
        for key in ('native_prompt', 'measured_prompt', 'executed_prompt', 'generation_request'):
            wrong = copy.deepcopy(rows); wrong[0][key] = 'different'
            with self.assertRaises(Exception): f.comparison_requests(wrong)

    def test_frozen_scope_cannot_expand_or_change_sampling_defaults(self):
        plan = dict(schema='q4t-realtime-speech-content-v1', limits=dict(f.LIMITS),
            no_replay_of_passed_protocol=True, sampling_defaults_changed=False,
            input_review=observed(), comparisons=f.comparison_requests(observed()))
        f.check_plan(plan)
        for change in [lambda v:v['limits'].update(starts=2),
                       lambda v:v.update(sampling_defaults_changed=True),
                       lambda v:v['comparisons']['pcm'].update(temperature=1),
                       lambda v:v.update(no_replay_of_passed_protocol=False)]:
            wrong = copy.deepcopy(plan);change(wrong)
            with self.assertRaises(Exception): f.check_plan(wrong)


if __name__ == '__main__': unittest.main()

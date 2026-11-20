"""Counterexamples for real audio interruption and actual tool provenance."""
import copy
import json
import unittest
from tools.serve_realtime import duplex_task_checks as c


def fixture():
    response = dict(id='response', status='cancelled', output_modalities=['audio'],
        output=[dict(type='message', id='spoken', content=[dict(type='output_audio')])],
        usage=dict(input_tokens=3, output_tokens=5, total_tokens=8,
                   output_token_details=dict(audio_tokens=3, text_tokens=2)))
    events = [dict(type='response.created', response=dict(id='response')),
        dict(type='response.output_audio.delta', response_id='response', item_id='spoken',
             output_index=0, content_index=0, delta='AQI='),
        dict(type='input_audio_buffer.speech_started', item_id='input'),
        dict(type='response.done', response=response),
        dict(type='input_audio_buffer.committed', item_id='input')]
    return events, response


class AudioBoundary(unittest.TestCase):
    def test_side_response_is_not_a_vad_cancellation_target(self):
        for conversation in ({}, {'conversation':'auto'}):
            c.default_response([dict(conversation, output_modalities=['audio'])])
        for wrong in ([], [dict(conversation='none', output_modalities=['audio'])],
                      [dict(conversation='auto', output_modalities=['text'])]):
            with self.assertRaises(RuntimeError): c.default_response(wrong)

    def test_actual_audio_then_vad_cancellation(self):
        events, response = fixture()
        self.assertEqual(c.barge_in(events, response), 'input')
        self.assertEqual(c.c.speech_checks.usage(response), 8)

    def test_completed_early_is_not_interrupted(self):
        events, response = fixture()
        response['status'] = 'completed'
        with self.assertRaises(RuntimeError): c.barge_in(events, response)
        events, response = fixture()
        events[2], events[3] = events[3], events[2]
        with self.assertRaises(RuntimeError): c.barge_in(events, response)

    def test_no_text_only_or_foreign_audio_can_pass(self):
        for field, value in [('type','response.output_text.delta'), ('delta',''),
                             ('response_id','foreign'), ('item_id','foreign'),
                             ('output_index',1), ('content_index',1)]:
            events, response = fixture();events[1][field] = value
            with self.assertRaises(RuntimeError): c.barge_in(events, response)

    def test_duplicate_terminal_and_late_audio_fail(self):
        events, response = fixture()
        events.append(copy.deepcopy(events[3]))
        with self.assertRaises(RuntimeError): c.barge_in(events, response)
        events, response = fixture()
        events.append(copy.deepcopy(events[1]))
        with self.assertRaises(RuntimeError): c.barge_in(events, response)

    def test_cancelled_work_and_input_identity_are_not_discarded(self):
        events, response = fixture()
        response['usage']['output_token_details']['audio_tokens'] = 0
        with self.assertRaises(RuntimeError): c.barge_in(events, response)
        events, response = fixture()
        events[-1]['item_id'] = 'foreign'
        with self.assertRaises(RuntimeError): c.barge_in(events, response)


class ToolBoundary(unittest.TestCase):
    def test_tool_source_call_and_return_must_match(self):
        record = dict(version='rev376', sha256='a'*64)
        actual = dict(record_key='release', version='rev376',
                      source='docs/STATUS.md', source_sha256='a'*64)
        responses = dict(tool=dict(output=[dict(type='function_call', id='call_item',
            call_id='call', name='lookup_project_record', arguments='{"record_key":"release"}')]))
        event = dict(type='conversation.item.done', item=dict(id='release_result',
            type='function_call_output', call_id='call', output=json.dumps(actual)))
        self.assertEqual(c.tool_result([event], responses, actual, record), 'call_item')
        for changed in ('call', 'source', 'content', 'duplicate'):
            bad = copy.deepcopy(event);truth = copy.deepcopy(record)
            if changed == 'call': bad['item']['call_id'] = 'foreign'
            elif changed == 'source': truth['sha256'] = 'b'*64
            elif changed == 'content': bad['item']['output'] = '{}'
            with self.assertRaises(RuntimeError):
                c.tool_result([bad, bad] if changed == 'duplicate' else [bad],
                              responses, actual, truth)

    def test_wrong_model_answer_remains_failed(self):
        row = c.c.judgment('tool_answer', 'old audio answer', 'rev376')
        self.assertEqual(row['status'], 'FAIL')
        self.assertEqual(row['actual'], 'old audio answer')


if __name__ == '__main__': unittest.main()

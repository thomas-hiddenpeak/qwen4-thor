"""Negative controls for audio accounting and existing JSON/SSE readers."""
import copy
import io
import json
import os
import unittest
from unittest.mock import patch
import base64
from pathlib import Path

from tools.native_asr.chat_acceptance import usage
from tools.native_asr.chat_acceptance import materialize, requests, stored_history, WORK
from tools.native_asr.chat_acceptance import english_result, tool_result, silence_result
from tools.examples.chat_client import read_json, read_sse


def observed():
    return dict(prompt_tokens=100, completion_tokens=12, total_tokens=112,
        prompt_tokens_details=dict(cached_tokens=0, audio_tokens=12),
        q4t_audio_preparation=dict(input_tokens=20, output_tokens=5, audio_tokens=12))


class ChatAcceptance(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('Q4T_CHAT_AUDIO_MULTIMODAL'),
                         'explicit retained multimodal HTTP directory required')
    def test_real_mp3_and_tool_answers_replay_and_wrong_meaning_is_rejected(self):
        path = Path(os.environ['Q4T_CHAT_AUDIO_MULTIMODAL'])
        sdk = json.loads((path/'js-en-mp3.sdk.json').read_text())
        wire = (path/'js-en-mp3.wire').read_bytes()
        self.assertIn('writing', english_result(wire, sdk['result'])['activity'])
        changed = copy.deepcopy(sdk['result'])
        changed['events'] = [x for x in changed['events'] if not x.get('usage')]
        with self.assertRaises(RuntimeError): english_result(wire, changed)
        value = json.loads((path/'two-audio-tool-final.wire').read_text())
        result = json.loads((path/'tool-execution.json').read_text())['result']
        self.assertEqual(tool_result(value, result)['answer']['zh_topic'], '交易')
        changed = copy.deepcopy(value)
        answer = json.loads(changed['choices'][0]['message']['content'])
        answer['zh_topic'] = '天气'
        changed['choices'][0]['message']['content'] = json.dumps(answer)
        with self.assertRaises(RuntimeError): tool_result(changed, result)

    def test_silence_refusal_null_or_truncated_answer_is_a_quality_failure(self):
        value = dict(usage=observed(), choices=[dict(finish_reason='stop',
            message=dict(role='assistant', content='OK'))])
        self.assertEqual(silence_result(value), observed())
        for change in ('null', 'refusal', 'truncated', 'wrong'):
            bad = copy.deepcopy(value)
            choice = bad['choices'][0]
            if change == 'null': choice['message']['content'] = None
            elif change == 'refusal': choice['message']['refusal'] = 'cannot answer'
            elif change == 'truncated': choice['finish_reason'] = 'length'
            else: choice['message']['content'] = 'No'
            with self.assertRaisesRegex(RuntimeError, 'silence blocked text'):
                silence_result(bad)

    @unittest.skipUnless(os.environ.get('Q4T_CHAT_AUDIO_RETAINED'),
                         'explicit retained HTTP replay directory required')
    def test_real_stored_audio_parts_match_original_and_changed_source_is_rejected(self):
        path = Path(os.environ['Q4T_CHAT_AUDIO_RETAINED']) / 'stored-audio-history.wire'
        value = json.loads(path.read_text())
        result = stored_history(value, requests()['zh'])
        self.assertTrue(result['originals_equal'])
        self.assertEqual(result['messages'], 1)
        self.assertEqual(len(result['audio']), 1)
        for change in ('drop', 'replace', 'summary'):
            bad = copy.deepcopy(value)
            row = bad['data'][0]
            if change == 'drop': row['content_parts'].pop()
            elif change == 'replace': row['content_parts'][1]['input_audio']['data'] = 'AAAA'
            else: row['content'] = 'replaced'
            with self.assertRaises(RuntimeError): stored_history(bad, requests()['zh'])

    def test_only_source_bound_audio_markers_expand_and_originals_stay_small(self):
        rows = requests()
        original = copy.deepcopy(rows)
        self.assertLess(len(json.dumps(rows)), 16384)
        with patch.object(Path, 'read_bytes', return_value=b'original source'):
            prepared = materialize(rows['zh'])
            value = prepared['messages'][0]['content'][1]['input_audio']['data']
            self.assertEqual(base64.b64decode(value), b'original source')
            self.assertEqual(materialize(rows['invalid']), rows['invalid'])
        bad = copy.deepcopy(rows['zh'])
        bad['messages'][0]['content'][1]['input_audio']['data'] = 'fixture:other'
        with self.assertRaisesRegex(RuntimeError, 'unknown frozen'):
            materialize(bad)
        self.assertEqual(rows, original)

    def test_actual_preparation_required_and_reservations_cannot_replace_usage(self):
        value = dict(usage=observed())
        self.assertEqual(usage(value), usage(dict(usage=dict(observed(),
            completion_tokens_details=None))))
        bad = []
        for key, field in [('prompt_tokens', -1), ('total_tokens', 99),
                           ('completion_tokens', True)]:
            item = observed(); item[key] = field; bad.append(item)
        for key, value in [('input_tokens', 100), ('output_tokens', 12),
                           ('audio_tokens', 0), ('audio_tokens', 13)]:
            item = observed(); item['q4t_audio_preparation'][key] = value; bad.append(item)
        for item in bad:
            with self.assertRaises(RuntimeError): usage(dict(usage=item))

    def test_json_and_sse_keep_actual_shared_usage_without_transcript_events(self):
        envelope = dict(id='fixture-1', model='qwen3.8-flash-next')
        value = dict(envelope, object='chat.completion', choices=[dict(index=0,
            message=dict(role='assistant', content='OK'), finish_reason='stop')],
            usage=observed())
        parsed = read_json(io.BytesIO(json.dumps(value).encode()))
        chunks = [dict(envelope, object='chat.completion.chunk', choices=[dict(index=0,
            delta=dict(role='assistant', content='OK'), finish_reason=None)]),
            dict(envelope, object='chat.completion.chunk', choices=[dict(index=0,
                delta={}, finish_reason='stop')]),
            dict(envelope, object='chat.completion.chunk', choices=[], usage=observed())]
        raw = b''.join(('data: '+json.dumps(chunk)+'\n\n').encode() for chunk in chunks)
        streamed = read_sse(io.BytesIO(raw+b'data: [DONE]\n\n'))
        self.assertEqual(parsed.message, streamed.message)
        self.assertEqual(parsed.usage, streamed.usage)
        self.assertEqual(usage(dict(usage=parsed.usage)), observed())


if __name__ == '__main__': unittest.main()

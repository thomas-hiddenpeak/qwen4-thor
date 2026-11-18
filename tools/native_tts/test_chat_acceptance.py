import base64
import copy
import json
import unittest

from tools.native_tts.chat_acceptance import check_usage, parse_stream


def usage():
    return dict(prompt_tokens=100,completion_tokens=100,total_tokens=200,
        q4t_audio_synthesis=dict(input_tokens=12,output_tokens=65),
        completion_tokens_details=dict(audio_tokens=64))


def events():
    def event(delta, **extra):
        return dict(object='chat.completion.chunk',choices=[dict(index=0,delta=delta,**extra)])
    return [event(dict(role='assistant'),finish_reason=None),
        event(dict(audio=dict(id='audio_1')),finish_reason=None),
        event(dict(audio=dict(transcript='actual utterance')),finish_reason=None),
        event(dict(audio=dict(data=base64.b64encode(b'actual').decode())),finish_reason=None),
        event(dict(audio=dict(expires_at=12345))),
        event({},finish_reason='stop'),
        dict(object='chat.completion.chunk',choices=[],usage=usage())]


def wire(value):
    return ''.join('data: '+json.dumps(row)+'\r\n\r\n' for row in value).encode()+b'data: [DONE]\r\n\r\n'


class Contract(unittest.TestCase):
    def test_actual_audio_bytes_usage_and_expiry_survive_reassembly(self):
        choices,actual,parsed=parse_stream(wire(events()),1)
        self.assertEqual(base64.b64decode(choices[0]['data']),b'actual')
        self.assertEqual(parsed,events())
        check_usage(actual,1)

    def test_expiry_finish_order_and_partial_stream_cannot_pass(self):
        original=events()
        for mutate in (
            lambda rows: rows[4]['choices'][0].update(finish_reason=None),
            lambda rows: rows.insert(5,copy.deepcopy(rows[3])),
            lambda rows: rows[5]['choices'][0].update(finish_reason='length'),
            lambda rows: rows[3]['choices'][0].update(index=1)):
            rows=copy.deepcopy(original);mutate(rows)
            with self.assertRaises((AssertionError,RuntimeError,ValueError)): parse_stream(wire(rows),1)
        with self.assertRaises((AssertionError,RuntimeError,ValueError)):
            parse_stream(wire(original).replace(b'data: [DONE]\r\n\r\n',b''),1)

    def test_intermediate_base64_padding_cannot_hide_later_audio(self):
        rows=events()
        rows[3]['choices'][0]['delta']['audio']['data']='eA==eQ=='
        with self.assertRaises((AssertionError,RuntimeError,ValueError)):
            parse_stream(wire(rows),1)

    def test_usage_must_contain_real_codes_eos_and_primary_work(self):
        for change in (
            lambda row: row.update(total_tokens=201),
            lambda row: row['q4t_audio_synthesis'].update(output_tokens=64),
            lambda row: row['completion_tokens_details'].update(audio_tokens=65),
            lambda row: row['q4t_audio_synthesis'].update(input_tokens=100)):
            value=usage();change(value)
            with self.assertRaises((AssertionError,RuntimeError,ValueError)): check_usage(value,1)


if __name__=='__main__': unittest.main()

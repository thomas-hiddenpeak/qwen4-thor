"""Prediction accounting cannot be mistaken for extra visible tokens."""
import copy
import io
import json
import unittest

from tools.examples.chat_client import ProtocolError, read_json, read_sse


def body():
    return dict(id='p', object='chat.completion', model='m', created=1,
        choices=[dict(index=0, finish_reason='stop', logprobs=None,
                      message=dict(role='assistant', content='ok'))],
        usage=dict(prompt_tokens=5, completion_tokens=7, total_tokens=12,
            completion_tokens_details=dict(reasoning_tokens=1,
                accepted_prediction_tokens=4, rejected_prediction_tokens=2)))


class PredictionUsage(unittest.TestCase):
    def test_json_and_stream_preserve_prediction_details(self):
        data=body()
        value=read_json(io.BytesIO(json.dumps(data).encode()))
        events=[]
        for delta,finish in [(dict(role='assistant', content=''),None),
                (dict(content='ok'),None),({},'stop')]:
            events.append(dict(id='p', object='chat.completion.chunk', model='m',
                created=1, choices=[dict(index=0, delta=delta, finish_reason=finish,
                logprobs=None)], usage=None))
        events.append(dict(events[0], choices=[], usage=data['usage']))
        wire=(''.join('data: '+json.dumps(e)+'\r\n\r\n' for e in events)+'data: [DONE]\r\n\r\n').encode()
        self.assertEqual(value,read_sse(io.BytesIO(wire)))
        self.assertEqual(value.usage['completion_tokens'],7)

    def test_prediction_counts_must_be_nonnegative_and_consistent(self):
        for changes in (dict(accepted_prediction_tokens=-1),
                dict(rejected_prediction_tokens=True), dict(accepted_prediction_tokens=6),
                dict(rejected_prediction_tokens=None)):
            data=body();data['usage']['completion_tokens_details'].update(changes)
            with self.assertRaises(ProtocolError):
                read_json(io.BytesIO(json.dumps(data).encode()))
        data=body();del data['usage']['completion_tokens_details']['accepted_prediction_tokens']
        with self.assertRaises(ProtocolError):
            read_json(io.BytesIO(json.dumps(data).encode()))


if __name__=='__main__':unittest.main()

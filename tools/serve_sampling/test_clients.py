"""CPU protocol checks for independent choices and true token probabilities."""
import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.examples.chat_client import read_json, read_sse, ProtocolError, MultiCompletion, prepare_tool_calls

COUNTS = dict(prompt_tokens=10, completion_tokens=4, total_tokens=14)
PROBS = dict(content=[dict(token='�', logprob=-9999, bytes=[228], top_logprobs=[])], refusal=None)


def choice(index=0):
    return dict(index=index, message=dict(role='assistant', content='中'),
                finish_reason='stop', logprobs=PROBS)


def complete(indices=(0, 1)):
    return dict(id='c', object='chat.completion', model='m', created=1,
                choices=[choice(i) for i in indices], usage=COUNTS)


def stream():
    result = []
    for index in (1, 0):
        for delta, finish, probabilities in ((dict(role='assistant', content=''), None, None),
                (dict(content='中'), None, PROBS), ({}, 'stop', None)):
            result.append(dict(id='c', model='m', created=1, object='chat.completion.chunk',
                choices=[dict(index=index, delta=delta, finish_reason=finish, logprobs=probabilities)], usage=None))
    result.append(dict(result[0], choices=[], usage=COUNTS))
    return result


def wire(events):
    return io.BytesIO((''.join('data: '+json.dumps(e)+'\r\n\r\n' for e in events) +
                        'data: [DONE]\r\n\r\n').encode())


class Choices(unittest.TestCase):
    def test_multi_choice_requires_explicit_selection_before_tools(self):
        value = read_json(io.BytesIO(json.dumps(complete()).encode()))
        with self.assertRaisesRegex(ProtocolError, 'select an explicit choice'):
            prepare_tool_calls(value, {})

    def test_json_stream_preserve_choices_and_probabilities(self):
        a = read_json(io.BytesIO(json.dumps(complete()).encode()))
        b = read_sse(wire(stream()))
        self.assertIsInstance(a, MultiCompletion)
        self.assertEqual(a, b)
        self.assertEqual(a.select(1).logprobs, PROBS)
        self.assertEqual(a.select(1).usage, COUNTS)
        with self.assertRaises(ProtocolError):
            a.select(-1)

    def test_incomplete_choice_cannot_succeed(self):
        events = stream()
        del events[-2]
        with self.assertRaises(ProtocolError):
            read_sse(wire(events))
        for indices in ((0, 0), (1,), (0, 2)):
            with self.assertRaises(ProtocolError):
                read_json(io.BytesIO(json.dumps(complete(indices)).encode()))

    def test_probability_corruption_is_rejected(self):
        for key, value in (('logprob', 1), ('bytes', [256]), ('top_logprobs', None)):
            body = json.loads(json.dumps(complete((0,))))
            body['choices'][0]['logprobs']['content'][0][key] = value
            with self.assertRaises(ProtocolError):
                read_json(io.BytesIO(json.dumps(body).encode()))

    def test_thinking_usage_sums_across_choices(self):
        body = complete()
        body['usage'] = dict(prompt_tokens=10, completion_tokens=20, total_tokens=30,
            completion_tokens_details=dict(reasoning_tokens=12),
            q4t_thinking_budget=dict(limit=5, consumed=10, forced_closure_tokens=2))
        result = read_json(io.BytesIO(json.dumps(body).encode()))
        self.assertEqual(result.select(0).usage['completion_tokens_details']['reasoning_tokens'], 12)


if __name__ == '__main__':
    unittest.main()

"""Run with the EvalScope venv; CPU-only fake HTTP, no CUDA or PMU."""
import asyncio
import json
import unittest
from types import SimpleNamespace

from evalscope.perf.plugin.api.default_api import DefaultApiPlugin
from ucf_client_boundary import instrument


class Response:
    status = 200
    headers = {'Content-Type': 'text/event-stream'}

    def __init__(self, chunks):
        self.chunks = chunks
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def iter_any(self):
        for chunk in self.chunks:
            yield chunk


class Session:
    def __init__(self, chunks, calls):
        self.chunks, self.calls = chunks, calls

    def post(self, **kwargs):
        self.calls.append('post')
        return Response(self.chunks)


def message(content):
    return ('data: ' + json.dumps({'choices': [{'delta': content}]}) + '\n\n').encode()


class BoundaryTests(unittest.TestCase):
    def run_case(self, chunks):
        calls = []
        observed = []
        def start():
            calls.append('start')
        def first(st, timestamp):
            calls.append('first')
            observed.append(timestamp - st)
        fn = instrument(DefaultApiPlugin.process_request, start, first)
        # Streaming path needs no plugin configuration for these response fields.
        plugin = SimpleNamespace()
        result = asyncio.run(fn(plugin, Session(chunks, calls), 'http://fake', {},
                                {'stream': True}))
        return calls, observed, result

    def test_empty_role_ping_fragmented_content_and_later_tokens(self):
        first = message({'content': 'A'})
        calls, observed, result = self.run_case([
            b': ping\n\n', message({'role': 'assistant'}),
            first[:13], first[13:], message({'content': 'B'}), b'data: [DONE]\n\n'])
        self.assertEqual(calls, ['start', 'post', 'first'])
        self.assertEqual(observed, [result.first_chunk_latency])
        self.assertEqual(result.generated_text, 'AB')
        self.assertTrue(result.success)

    def test_no_output_has_no_first_boundary(self):
        calls, observed, result = self.run_case([
            message({'role': 'assistant'}), b'data: [DONE]\n\n'])
        self.assertEqual(calls, ['start', 'post'])
        self.assertEqual(observed, [])
        self.assertEqual(result.first_chunk_latency, 0)

    def test_callback_failure_is_visible_in_evalscope_result(self):
        def broken(*args):
            raise RuntimeError('counter boundary failed')
        fn = instrument(DefaultApiPlugin.process_request, lambda: None, broken)
        result = asyncio.run(fn(SimpleNamespace(),
                                Session([message({'content': 'A'})], []),
                                'http://fake', {}, {'stream': True}))
        self.assertFalse(result.success)
        self.assertIn('counter boundary failed', result.error)

    def test_identity_change_fails_before_callbacks(self):
        with self.assertRaisesRegex(ValueError, 'identity mismatch'):
            instrument(DefaultApiPlugin.process_request, lambda: None,
                       lambda *_: None, expected_sha256='changed')


if __name__ == '__main__':
    unittest.main()

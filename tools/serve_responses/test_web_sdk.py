"""Pinned official Python SDK consuming real native CPU JSON/SSE captures."""
import json
import os
from pathlib import Path
import unittest

import httpx2
import openai

CAPTURES = Path(os.environ['Q4T_WEB_API_OUTPUT'])
assert openai.__version__ == '3.42.1', 'SDK version drift'


def client(name, stream, api):
    def reply(request):
        assert request.method == 'POST' and request.url.path == '/v1/' + api
        body = json.loads(request.content)
        if api == 'responses':
            assert body['tools'] == [dict(type='web_search', external_web_access=False)]
        else:
            assert body['web_search_options'] == {}
        assert body.get('stream', False) == stream
        return httpx2.Response(200, content=(CAPTURES / (name + ('.wire' if stream else '.json'))).read_bytes(),
            headers={'content-type': 'text/event-stream' if stream else 'application/json'})
    return openai.OpenAI(api_key='cpu', base_url='http://cpu.invalid/v1',
        max_retries=0, _strict_response_validation=True,
        http_client=httpx2.Client(transport=httpx2.MockTransport(reply), trust_env=False))


class WebSdk(unittest.TestCase):
    def test_responses_json_sse_and_history_shape(self):
        for name in ('response', 'response-included'):
            request = dict(model='cpu', input='Find alpha', tools=[dict(type='web_search', external_web_access=False)])
            expected = json.loads((CAPTURES / (name + '.json')).read_text())
            with client(name, False, 'responses') as sdk:
                response = sdk.responses.create(**request)
                self.assertEqual(response.output[0].action.type, 'search')
                self.assertEqual(response.output[1].content[0].annotations[0].url, 'https://example.com/source')
            with client(name, True, 'responses') as sdk:
                with sdk.responses.stream(**request) as stream:
                    events = list(stream)
                    response = stream.get_final_response()
                self.assertEqual(response.status, 'completed')
                self.assertEqual(response.output_text, expected['output'][1]['content'][0]['text'])
                self.assertEqual([e.sequence_number for e in events], list(range(len(events))))
                self.assertEqual(response.output[0].action.model_dump(exclude_unset=True), expected['output'][0]['action'])

    def test_chat_json_and_stream_preserve_citation(self):
        request = dict(model='cpu', messages=[dict(role='user', content='Find alpha')], web_search_options={})
        expected = json.loads((CAPTURES / 'chat.json').read_text())['choices'][0]['message']
        with client('chat', False, 'chat/completions') as sdk:
            response = sdk.chat.completions.create(**request)
            self.assertEqual(response.choices[0].message.model_dump(exclude_unset=True), expected)
        with client('chat', True, 'chat/completions') as sdk:
            chunks = list(sdk.chat.completions.create(**request, stream=True))
            annotations = [a for c in chunks for choice in c.choices
                for a in choice.delta.model_dump(exclude_unset=True).get('annotations', [])]
            self.assertEqual(annotations, expected['annotations'])
            self.assertEqual(''.join(choice.delta.content or '' for c in chunks for choice in c.choices), expected['content'])


if __name__ == '__main__':
    unittest.main()

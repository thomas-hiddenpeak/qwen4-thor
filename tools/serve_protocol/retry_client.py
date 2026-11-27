"""Optional OpenAI Python SDK peer for the gateway retry contract."""
import json
import sys

import openai


def main():
    settings = json.loads(sys.stdin.readline())
    attempts = []
    restoring = False

    def observe(response):
        attempts.append(response.status_code)
        if restoring and response.status_code == settings['status']:
            print(json.dumps({'rejected': response.status_code}), flush=True)
            if sys.stdin.readline().strip() != 'ready':
                raise RuntimeError('test peer did not recover')

    body = dict(model='cpu-contract', messages=[dict(role='user', content='one request')])
    with openai.DefaultHttpxClient(event_hooks={'response': [observe]}, trust_env=False) as transport:
        client = openai.OpenAI(api_key=settings['key'], base_url=settings['base'],
                               http_client=transport, max_retries=1, timeout=5)
        try:
            first = client if settings['accepted'] else client.with_options(max_retries=0)
            first.chat.completions.create(**body)
        except openai.APIStatusError as exc:
            headers = exc.response.headers
            error = dict(status=exc.status_code, name=type(exc).__name__, code=exc.code,
                         type=exc.type, param=exc.param, trace=exc.request_id,
                         headerTrace=headers.get('x-request-id'), delay=headers.get('retry-after'),
                         retry=headers.get('x-should-retry'))
        else:
            raise AssertionError('expected an API error')
        reply = None
        if not settings['accepted']:
            restoring = True
            result = client.chat.completions.create(**body)
            reply = result.choices[0].message.content
        print(json.dumps(dict(version=openai.__version__, attempts=attempts,
                              error=error, reply=reply)), flush=True)


if __name__ == '__main__':
    main()

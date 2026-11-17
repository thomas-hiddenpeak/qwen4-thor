"""One real pinned Chat SDK request with bounded raw response capture."""
import base64
import json
import sys

import httpx
import openai

from transcription_sdk import Transport


def main():
    raw = sys.stdin.read(16 * 1024 * 1024 + 1)
    if len(raw) > 16 * 1024 * 1024:
        raise RuntimeError('request capture capacity')
    value = json.loads(raw)
    if openai.__version__ != '2.8.1':
        raise RuntimeError('Python SDK version drift')
    transport = Transport()
    result = dict(ok=False, version=openai.__version__)
    with httpx.Client(transport=transport, trust_env=False, timeout=180) as http:
        client = openai.OpenAI(base_url=value['base_url'], api_key='local',
            max_retries=0, timeout=180, http_client=http)
        try:
            body = dict(value['request'])
            extra = {k: body.pop(k) for k in ('enable_thinking', 'request_id',
                'cancel_token') if k in body}
            answer = client.chat.completions.create(**body, extra_body=extra,
                extra_headers=value.get('headers', {}))
            if body.get('stream'):
                with answer:
                    result['result'] = dict(events=[event.model_dump(mode='json')
                        for event in answer])
            else:
                result['result'] = answer.model_dump(mode='json')
            result['ok'] = True
        except Exception as error:
            result['error'] = dict(type=type(error).__name__, message=str(error),
                status=getattr(error, 'status_code', None))
    result.update(exchanges=transport.exchanges,
        wire_base64=base64.b64encode(b''.join(transport.wire)).decode())
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

"""One real pinned Python SDK request, with bounded incremental wire capture."""
import base64
import json
import ssl
import sys

import httpx
import openai


class Capture(httpx.SyncByteStream):
    def __init__(self, stream, owner):
        self.stream, self.owner = stream, owner

    def __iter__(self):
        for chunk in self.stream:
            self.owner.bytes += len(chunk)
            if self.owner.bytes > 4 * 1024 * 1024:
                raise RuntimeError('transcription wire limit')
            self.owner.wire.append(chunk)
            yield chunk

    def close(self):
        self.stream.close()


class Transport(httpx.BaseTransport):
    def __init__(self, verify=True):
        self.transport = httpx.HTTPTransport(retries=0, verify=verify)
        self.wire, self.exchanges, self.bytes = [], [], 0

    def handle_request(self, request):
        self.exchanges.append(dict(method=request.method, url=str(request.url)))
        response = self.transport.handle_request(request)
        return httpx.Response(response.status_code, headers=response.headers,
            stream=Capture(response.stream, self), extensions=response.extensions,
            request=request)

    def close(self):
        self.transport.close()


def main():
    value = json.loads(sys.stdin.read(1024 * 1024 + 1))
    if openai.__version__ != '2.8.1':
        raise RuntimeError('Python SDK version drift')
    transport = Transport(ssl.create_default_context(cafile=value['ca'])
                          if value.get('ca') else True)
    result = dict(ok=False, version=openai.__version__)
    with httpx.Client(transport=transport, trust_env=False, timeout=180) as http:
        client = openai.OpenAI(base_url=value['base_url'],
                               api_key=value.get('api_key', 'local'),
                               max_retries=0, timeout=180, http_client=http)
        try:
            with open(value['audio'], 'rb') as audio:
                answer = client.audio.transcriptions.create(
                    file=(value['filename'], audio, 'audio/wav'), **value['request'])
                if value['request'].get('stream'):
                    with answer:
                        result['result'] = dict(events=[event.model_dump(mode='json')
                                                       for event in answer])
                else:
                    result['result'] = answer if isinstance(answer, str) else answer.model_dump(mode='json')
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

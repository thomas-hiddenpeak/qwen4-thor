"""One pinned official Voice/Speech/Chat SDK call against an owned local server.

This is a test client, never a runtime dependency or a substitute SDK. TLS
verification and the original API methods stay enabled; requests never retry.
"""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import ssl
import sys
from urllib.parse import urlparse

VERSION = '3.55.1'
REQUEST_LIMIT = (10 << 20) + 65536
RESPONSE_LIMIT = 4 << 20


def execute(config, *, transport=None):
    import httpx2 as httpx
    import openai

    if openai.__version__ != VERSION:
        raise RuntimeError('Voice SDK version drift')
    parsed = urlparse(config['base_url'])
    if (parsed.hostname not in ('127.0.0.1', '::1') or
            parsed.scheme not in ('http', 'https') or parsed.username or
            parsed.password or parsed.query or parsed.fragment):
        raise ValueError('owned loopback endpoint required')
    exchanges, wire, request_wire = [], [], []

    class Capture(httpx.SyncByteStream):
        def __init__(self, source):
            self.source, self.count = source, 0

        def __iter__(self):
            for chunk in self.source:
                self.count += len(chunk)
                if self.count > RESPONSE_LIMIT:
                    raise RuntimeError('Voice SDK response byte bound')
                wire.append(chunk)
                yield chunk

        def close(self):
            self.source.close()

    class BoundedTransport(httpx.BaseTransport):
        def __init__(self):
            verify = (ssl.create_default_context(cafile=config['ca'])
                      if config.get('ca') else True)
            self.inner = (transport if transport is not None else
                          httpx.HTTPTransport(retries=0, verify=verify))

        def handle_request(self, request):
            if exchanges:
                raise RuntimeError('Voice SDK automatic replay forbidden')
            # This test client owns <=10MiB fixed inputs. Capture exact multipart
            # bytes without logging Authorization or changing SDK serialization.
            raw = request.read()
            if len(raw) > REQUEST_LIMIT:
                raise RuntimeError('Voice SDK request byte bound')
            request_wire.append(raw)
            row = dict(method=request.method, url=str(request.url),
                       content_type=request.headers.get('content-type', ''),
                       request_bytes=len(raw),
                       request_sha256=hashlib.sha256(raw).hexdigest())
            exchanges.append(row)
            response = self.inner.handle_request(request)
            row.update(status=response.status_code,
                       response_headers=dict(response.headers))
            return httpx.Response(response.status_code, headers=response.headers,
                stream=Capture(response.stream), extensions=response.extensions,
                request=request)

        def close(self):
            self.inner.close()

    def dump(value):
        return value.model_dump(mode='json', exclude_unset=True)

    result = dict(ok=False, version=VERSION, exchanges=exchanges)
    with ExitStack() as stack:
        capture = BoundedTransport()
        http = stack.enter_context(httpx.Client(transport=capture,
                    trust_env=False, timeout=180))
        client = openai.OpenAI(base_url=config['base_url'],
            api_key=config['api_key'], max_retries=0, timeout=180,
            http_client=http)
        body = dict(config.get('request', {}))
        action = config['action']
        if action in ('consent_create', 'voice_create', 'transcribe'):
            audio = config['audio']; path = Path(audio['path'])
            if not path.is_absolute() or not 0 < path.stat().st_size <= 10 << 20:
                raise ValueError('invalid fixed SDK input')
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != audio['sha256']:
                raise ValueError('Voice SDK input identity drift')
            field = {'consent_create': 'recording', 'voice_create': 'audio_sample',
                     'transcribe': 'file'}[action]
            body[field] = (path.name, raw, 'audio/wav')
        try:
            if action == 'consent_create':
                answer = client.audio.voice_consents.create(**body)
            elif action == 'consent_list':
                answer = client.audio.voice_consents.list(**body)
            elif action == 'consent_retrieve':
                answer = client.audio.voice_consents.retrieve(**body)
            elif action == 'consent_update':
                answer = client.audio.voice_consents.update(**body)
            elif action == 'consent_delete':
                answer = client.audio.voice_consents.delete(**body)
            elif action == 'voice_create':
                answer = client.audio.voices.create(**body)
            elif action == 'voice_delete':
                answer = client.audio.voices.delete(**body)
            elif action == 'speech':
                chunks = []
                with client.audio.speech.with_streaming_response.create(**body) as response:
                    chunks.extend(response.iter_bytes())
                    result.update(status=response.status_code,
                                  headers=dict(response.headers))
                data = b''.join(chunks)
                if data != b''.join(wire):
                    raise RuntimeError('SDK changed speech bytes')
                result['result'] = dict(bytes=len(data),
                    sha256=hashlib.sha256(data).hexdigest())
                answer = None
            elif action == 'chat':
                # These q4t extensions are not official SDK keyword arguments.
                # extra_body preserves them in the actual JSON request body.
                extra = {key: body.pop(key) for key in
                         ('enable_thinking', 'request_id', 'cancel_token')
                         if key in body}
                answer = client.chat.completions.create(**body, extra_body=extra)
                if body.get('stream'):
                    with answer:
                        result['result'] = dict(events=[dump(event) for event in answer])
                    answer = None
            elif action == 'chat_retrieve':
                answer = client.chat.completions.retrieve(**body)
            elif action == 'transcribe':
                answer = client.audio.transcriptions.create(**body)
            else:
                raise ValueError('unknown Voice SDK action')
            if answer is not None:
                result['result'] = answer if isinstance(answer, str) else dump(answer)
            if len(exchanges) != 1:
                raise RuntimeError('expected one actual SDK exchange')
            result['ok'] = True
        except Exception as error:
            result['error'] = dict(type=type(error).__name__, message=str(error),
                                  status=getattr(error, 'status_code', None))
    return result, b''.join(wire), b''.join(request_wire)


def main():
    raw = sys.stdin.buffer.read(65537)
    if len(raw) > 65536:
        raise ValueError('SDK configuration byte bound')
    config = json.loads(raw)
    output = Path(config['output'])
    root = Path(__file__).resolve().parents[2]/'.q4t-work'
    if not output.is_absolute() or not output.resolve().is_relative_to(root):
        raise ValueError('private SDK output required')
    result, wire, request = execute(config)
    output.write_bytes(wire)
    output.with_suffix(output.suffix+'.request').write_bytes(request)
    result.update(wire_bytes=len(wire), wire_sha256=hashlib.sha256(wire).hexdigest())
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

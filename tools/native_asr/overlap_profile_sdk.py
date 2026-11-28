"""Read a real mixed-voice transcription using the pinned typed Python client."""
import hashlib
import json
from pathlib import Path
import ssl
import sys
from urllib.parse import urlsplit

from openai import DefaultHttpxClient, OpenAI, __version__


def transcribe(client, sample, request):
    with sample.open('rb') as stream:
        audio = stream.read(128045)
    if len(audio) != 128044:
        raise ValueError('expected the selected four-second WAV')
    raw = client.audio.transcriptions.with_raw_response.create(
        file=('mixed.wav', audio, 'audio/wav'), **request)
    typed = raw.parse()
    output = json.loads(raw.content)
    labels = [segment.speaker for segment in typed.segments]
    if typed.text != output['text'] or labels != [s['speaker'] for s in output['segments']]:
        raise ValueError('SDK transcript differs from the wire response')
    return dict(sdk=__version__, request=request, source_sha256=hashlib.sha256(audio).hexdigest(),
                wire=raw.content.decode('utf-8'), labels=labels, wire_sha256=hashlib.sha256(raw.content).hexdigest(),
                method=raw.http_request.method, path=raw.http_request.url.path,
                status=raw.status_code, trace=raw.headers.get('x-request-id'))


def main():
    plan = json.load(sys.stdin)
    url = urlsplit(plan['base'])
    if (url.scheme, url.hostname, url.path) != ('https', '127.0.0.1', '/v1'):
        raise ValueError('expected the private native TLS listener')
    sample = Path(plan['audio'])
    if __version__ != '3.55.1' or sample.stat().st_size != 128044:
        raise ValueError('expected the selected SDK and four-second WAV')
    with DefaultHttpxClient(verify=ssl.create_default_context(cafile=plan['ca']),
                            trust_env=False, timeout=100) as http:
        client = OpenAI(base_url=plan['base'], api_key=plan['key'], http_client=http,
                        timeout=100, max_retries=0)
        print(json.dumps(transcribe(client, sample, plan['request'])))


if __name__ == '__main__':
    main()

"""Bounded native TLS translation acceptance with a real Chromium WebRTC peer."""
import base64
import hashlib
import io
import json
from pathlib import Path
import re
import ssl
import sys
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import wave

from playwright.sync_api import sync_playwright


def run(plan):
    base = plan['base']
    assert urlsplit(base).hostname == '127.0.0.1' and base.startswith('https://')
    tls = ssl.create_default_context(cafile=plan['ca'])
    def post(path, key, body, content_type='application/json'):
        payload = (json.dumps(body).encode() if content_type == 'application/json' else body)
        request = Request(base + path, data=payload, headers={
            'Authorization': 'Bearer ' + key, 'Content-Type': content_type})
        try:
            with urlopen(request, context=tls, timeout=60) as reply:
                return reply.status, reply.read(4 << 20)
        except HTTPError as error:
            return error.code, error.read(4096)

    def api(path, body, key=None):
        status, wire = post(path, key or plan['key'], body)
        assert 200 <= status < 300, (status, wire[:500])
        return json.loads(wire)
    clip = Path(plan['clip']).read_bytes()
    assert hashlib.sha256(clip).hexdigest() == plan['sha256']
    grant = api('/realtime/translations/client_secrets', {'session': {
        'model':'gpt-realtime-translate',
        'audio':{'input':{'transcription':{'model':'gpt-realtime-whisper'}},
                 'output':{'language':'en'}}}})
    ordinary = api('/realtime/client_secrets', {'session':{'type':'realtime'}})
    swapped = [post(path, key, b'v=0\r\n', 'application/sdp')[0] for path, key in [
        ('/realtime/calls', grant['value']),
        ('/realtime/translations/calls', ordinary['value'])]]
    assert swapped == [401, 401], swapped
    origin = base.removesuffix('/v1')
    with sync_playwright() as pw, pw.chromium.launch(args=[
            '--disable-gpu', '--autoplay-policy=no-user-gesture-required',
            '--allow-loopback-in-peer-connection',
            '--ignore-certificate-errors-spki-list=' + plan['cert_spki']]) as browser:
        version = browser.version
        page = browser.new_page()
        page.route(origin + '/translation-test', lambda route: route.fulfill(
            status=200, content_type='text/html', body='<title>q4t media acceptance</title>'))
        page.goto(origin + '/translation-test', timeout=12000)
        result = page.evaluate(Path(__file__).with_suffix('.js').read_text(), {
            'grant':grant['value'], 'clip':base64.b64encode(clip).decode()})
    events = result.pop('messages')
    Path(plan['wire']).write_text(json.dumps(events, ensure_ascii=False))
    pcm = base64.b64decode(result.pop('pcm'), validate=True)
    assert 9600 < len(pcm) < 24000 * 2 * 30, len(pcm)
    with wave.open(plan['wav'], 'wb') as stream:
        stream.setparams((1, 2, 24000, 0, 'NONE', ''))
        stream.writeframes(pcm)
    transcript = lambda kind: ''.join(e['delta'] for e in events if e['type'] == kind)
    source = transcript('session.input_transcript.delta')
    english = transcript('session.output_transcript.delta')
    assert '交易' in source and events[-1]['type'] == 'session.closed', (source, events[-1])
    boundary = 'q4t-browser-recording'
    upload = io.BytesIO()
    for field, val in [('model','qwen3-asr-0.6b'),('language','en')]:
        upload.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"\r\n\r\n{val}\r\n'.encode())
    upload.write(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="remote.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode())
    upload.write(Path(plan['wav']).read_bytes())
    upload.write(f'\r\n--{boundary}--\r\n'.encode())
    status, wire = post('/audio/transcriptions', plan['key'], upload.getvalue(),
                        'multipart/form-data; boundary=' + boundary)
    assert status == 200, (status, wire[:500])
    spoken = json.loads(wire)['text']
    for text in [english, spoken]:
        assert re.search(r'trad(?:e|es|ing)|transaction', text, re.I) and re.search(
            r'stagn|halt|standstill|stop|stall|froze|frozen|suspend|cease|slow|grind|ground', text, re.I), text
    chat = api('/chat/completions', {'model':'qwen3.8-flash-next', 'temperature':0,
        'enable_thinking':False, 'max_tokens':16, 'messages':[{'role':'user',
        'content':'Return only the number equal to 6 times 7.'}]}, plan['other'])
    independent = chat['choices'][0]['message']['content'].strip()
    assert independent == '42' and result['receivedBeforeClose']
    return dict(result, browser=version, input=source, english=english, spoken=spoken,
                pcm_bytes=len(pcm), grant_audiences=swapped, independent_chat=independent,
                wire=events, wav_sha256=hashlib.sha256(Path(plan['wav']).read_bytes()).hexdigest())


if __name__ == '__main__':
    print(json.dumps(run(json.load(sys.stdin))))

"""Replay the native Live download capacity error and WAV through the installed SDK."""
import base64
import hashlib
import json
import time
from pathlib import Path

import openai

from .sdk_route_probe import httpx


def main():
    evidence = Path(__file__).resolve().parents[2] / (
        'docs/evidence/serve-sdk-method-matrix-20261127/recording-download-retries.json')
    capture = json.loads(evidence.read_bytes())
    expected, = (row for row in capture['clients'] if row['language'] == 'python'
                 and row['sdk'] == openai.__version__)
    packets = capture['packets'][1:3]
    times = []

    def reply(request):
        if (request.method != 'GET' or str(request.url) !=
                'http://sdk.invalid/v1/live/sessions/live_recorded/content'):
            raise ValueError('unexpected recording request')
        packet = packets[len(times)]
        times.append(time.monotonic())
        return httpx.Response(packet['status'], headers=packet['headers'], request=request,
                              content=base64.b64decode(packet['body'], validate=True))

    with httpx.Client(transport=httpx.MockTransport(reply), trust_env=False) as transport:
        with openai.OpenAI(api_key='fixture', base_url='http://sdk.invalid/v1',
                           max_retries=1, http_client=transport) as client:
            if expected['mode'] == 'resource':
                response = client.live.sessions.download_recording('live_recorded').response
            else:
                response = client.get('/live/sessions/live_recorded/content', cast_to=httpx.Response)
            if (len(times) != 2 or response.status_code != 200 or
                    response.headers['x-request-id'] != expected['trace'] or
                    hashlib.sha256(response.content).hexdigest() != expected['sha256']):
                raise ValueError('SDK did not recover the recorded WAV')
    delay = times[1] - times[0]
    if delay < int(packets[0]['headers']['Retry-After']) - 0.01:
        raise ValueError('SDK retried before the recording delay')
    print(json.dumps(dict(expected, wait_seconds=round(delay, 3))))


if __name__ == '__main__':
    main()

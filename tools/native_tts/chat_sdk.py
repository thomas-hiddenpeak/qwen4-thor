"""One actual Chat audio SDK request, bounded capture and no retries."""
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import httpx
import openai
from tools.native_asr.transcription_sdk import Transport


def main():
    value = json.loads(sys.stdin.read(65537))
    assert openai.__version__ == '2.8.1'
    capture = Transport()
    started = time.monotonic()
    with httpx.Client(transport=capture, trust_env=False, timeout=180) as http:
        client = openai.OpenAI(base_url=value['base_url'], api_key='local',
            default_headers=value['headers'], max_retries=0, timeout=180,
            http_client=http)
        request = dict(value['request'])
        extra = {}
        if 'enable_thinking' in request:
            extra['enable_thinking'] = request.pop('enable_thinking')
        answer = client.chat.completions.create(**request, extra_body=extra)
        if value['request'].get('stream'):
            events = []
            with answer:
                for event in answer:
                    events.append(event.model_dump(mode='json', exclude_unset=True))
                    assert len(events) <= 4096
            result = dict(events=events)
        else:
            result = answer.model_dump(mode='json')
    wire = b''.join(capture.wire)
    assert wire and len(capture.exchanges) == 1
    Path(value['output']).write_bytes(wire)
    print(json.dumps(dict(ok=True, result=result, version=openai.__version__,
        exchanges=capture.exchanges, seconds=time.monotonic()-started)))


if __name__ == '__main__':
    main()

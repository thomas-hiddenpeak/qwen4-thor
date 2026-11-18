"""One real Python Speech SDK stream; bounded bytes, no automatic retry."""
import hashlib
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
    transport = Transport()
    chunks = []
    started = time.monotonic()
    with httpx.Client(transport=transport, trust_env=False, timeout=180) as http:
        client = openai.OpenAI(base_url=value['base_url'], api_key='local',
            max_retries=0, timeout=180, http_client=http)
        with client.audio.speech.with_streaming_response.create(
                **value['request']) as response:
            for part in response.iter_bytes():
                assert sum(map(len, chunks)) + len(part) <= 2 << 20
                if part:
                    if not chunks: first = time.monotonic() - started
                    chunks.append(part)
            headers = dict(response.headers)
            status = response.status_code
    data = b''.join(chunks)
    assert data and len(transport.exchanges) == 1
    assert data == b''.join(transport.wire)
    Path(value['output']).write_bytes(data)
    print(json.dumps(dict(ok=True, version=openai.__version__, status=status,
        headers=headers, chunks=len(chunks), first_seconds=first,
        seconds=time.monotonic()-started, bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(), exchanges=transport.exchanges)))


if __name__ == '__main__':
    main()

"""Replay the recorded native base64 response to the SDK's default embeddings call."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys


def python_actor(config):
    import importlib
    import openai

    # Stock Python 2.x uses httpx and 3.x uses httpx2. Use the transport
    # from this client so the replay travels through its own response parser.
    httpx = importlib.import_module(openai.DefaultHttpxClient.__base__.__module__)

    calls = []
    def replay(request):
        assert request.method == 'POST' and request.url == 'https://replay.invalid/v1/embeddings'
        sent = json.loads(request.content)
        assert sent == config['request']
        calls.append(sent['encoding_format'])
        return httpx.Response(200, content=Path(config['wire']).read_bytes(),
                              headers={'Content-Type': 'application/json'})

    transport = httpx.Client(transport=httpx.MockTransport(replay), trust_env=False)
    with openai.OpenAI(base_url='https://replay.invalid/v1', api_key='fixture', http_client=transport,
                       max_retries=0) as client:
        response = client.embeddings.create(**config['arguments'])
        rows = []
        for row in response.data:
            assert isinstance(row.embedding, list)
            raw = struct.pack('<' + 'f' * len(row.embedding), *row.embedding)
            rows.append(dict(index=row.index, sha256=hashlib.sha256(raw).hexdigest()))
    return dict(sdk='python-' + openai.__version__, wire_formats=calls, rows=rows)


def run(capture, wire, sdks):
    from tools.serve_audit.render_sdk_matrix import VERSIONS, sha

    source = json.loads(capture.read_bytes())
    raw = json.loads(wire.read_bytes())
    sent, = source['exchanges']
    body = json.loads(sent['body'])
    assert source['ok'] and sent['method'] == 'POST' and sent['url'].endswith('/v1/embeddings')
    assert body['encoding_format'] == 'base64' and raw['model'] == body['model']
    original = source['result']
    if any(raw.get(k) != original.get(k) for k in ('model', 'object', 'usage')):
        raise ValueError('Original SDK response metadata differs from the captured wire')
    expected = []
    for encoded, decoded in zip(raw['data'], original['data'], strict=True):
        data = base64.b64decode(encoded['embedding'], validate=True)
        vector = decoded['embedding']
        if (not isinstance(vector, list) or not vector or encoded['index'] != decoded['index']
                or data != struct.pack('<' + 'f' * len(vector), *vector)):
            raise ValueError('Original SDK vector or index differs from the captured wire')
        expected.append(dict(index=encoded['index'], dimensions=len(vector),
                             sha256=hashlib.sha256(data).hexdigest()))
    assert [r['index'] for r in expected] == list(range(len(body['input']))) and len(expected) > 1
    assert set(sdks) == {f'{language}-{version}' for language, version in VERSIONS}
    config = dict(arguments={k:v for k,v in body.items() if k != 'encoding_format'},
                  request=body, wire=str(wire.resolve()))
    actors = {}
    for sdk, location in sdks.items():
        args = ([location, '-I', '-B', __file__, '--python-actor'] if sdk.startswith('python-')
                else ['node', Path(__file__).with_suffix('.mjs'), location])
        proc = subprocess.run(args, input=json.dumps(config).encode(), capture_output=True, timeout=30)
        if proc.returncode:
            raise RuntimeError(f'{sdk} replay failed: {proc.stderr.decode(errors="replace")}')
        result = json.loads(proc.stdout)
        digests = [dict(index=row['index'], sha256=row['sha256']) for row in expected]
        if result['sdk'] != sdk or result['wire_formats'] != ['base64'] or result['rows'] != digests:
            raise ValueError(f'{sdk}: SDK default encoding, vector order or FP32 content changed')
        actors[sdk] = dict(wire_format='base64', numeric_vectors=len(expected))
    return dict(status='SDK_REPLAY_PASS', transport='in-process replay of original native response bytes',
                new_backend_requests=0, model_starts=0, capture_sha256=sha(capture),
                wire_sha256=sha(wire), sdk=actors, expected_rows=expected)


if __name__ == '__main__':
    if sys.argv[1:] == ['--python-actor']:
        print(json.dumps(python_actor(json.load(sys.stdin)), allow_nan=False))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        for name in ('capture', 'wire', 'sdk_paths', 'output'):
            parser.add_argument(name, type=Path)
        args = parser.parse_args()
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        receipt = run(args.capture, args.wire, json.loads(args.sdk_paths.read_bytes()))
        args.output.write_text(json.dumps(receipt, indent=2) + '\n')

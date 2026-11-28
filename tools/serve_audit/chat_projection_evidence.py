"""Recheck recorded Chat JSON, SSE chunks and terminal errors against the pinned OpenAPI."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import subprocess

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[2]


def validators(spec):
    content = spec['paths']['/chat/completions']['post']['responses']['200']['content']
    schemas = {mime: row['schema'] for mime, row in content.items()}
    schemas['error'] = {'$ref': '#/components/schemas/ErrorResponse'}
    return {mime: Draft202012Validator(dict(components=spec['components'], **schema))
            for mime, schema in schemas.items()}


def check_value(schema, value):
    error = next(schema.iter_errors(value), None)
    if error:
        raise ValueError(f'OpenAPI {error.validator} at {error.json_path}')


def check_capture(raw, media, schemas):
    if media == 'application/json':
        check_value(schemas[media], json.loads(raw))
        return {'responses': 1}
    if media != 'text/event-stream':
        raise ValueError('unknown witness media type')
    # The recorded q4t Chat transport writes one data line per frame.
    frames = raw.replace(b'\r\n', b'\n').split(b'\n\n')
    if len(frames) < 3 or frames[-2:] != [b'data: [DONE]', b'']:
        raise ValueError('missing final DONE frame')
    counts = Counter()
    identity = None
    for frame in frames[:-2]:
        if not frame.startswith(b'data: ') or b'\n' in frame or frame == b'data: [DONE]':
            raise ValueError('invalid Chat witness frame')
        if counts['errors']:
            raise ValueError('data following terminal error')
        value = json.loads(frame[6:])
        if 'error' in value:
            check_value(schemas['error'], value)
            counts['errors'] += 1
        else:
            check_value(schemas[media], value)
            current = (value['id'], value['model'], value['created'])
            if identity is not None and current != identity:
                raise ValueError('Chat stream identity changed')
            identity = current
            counts['chunks'] += 1
    if not counts['chunks']:
        raise ValueError('witness contains no Chat output')
    counts['done'] = 1
    return dict(counts)


def check_manifest(spec_bytes, receipt):
    if hashlib.sha256(spec_bytes).hexdigest() != receipt['openapi_sha256']:
        raise ValueError('reference OpenAPI changed')
    schemas = validators(json.loads(spec_bytes))
    totals = Counter()
    for name, row in receipt['sources'].items():
        path = ROOT / name
        # Historical wire receipts also remain accessible from sparse checkouts.
        raw = path.read_bytes() if path.is_file() else subprocess.check_output(
            ['git', 'show', 'HEAD:'+name], cwd=ROOT)
        if hashlib.sha256(raw).hexdigest() != row['sha256']:
            raise ValueError('wire bytes changed: ' + name)
        counts = check_capture(raw, row['media'], schemas)
        if counts != row['counts']:
            raise ValueError('wire counts changed: ' + name)
        totals.update(counts)
    if totals != receipt['totals']:
        raise ValueError('combined Chat witness counts differ')
    return dict(totals)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('openapi', type=Path)
    parser.add_argument('receipt', type=Path)
    args = parser.parse_args()
    print(json.dumps(check_manifest(args.openapi.read_bytes(), json.loads(args.receipt.read_bytes()))))

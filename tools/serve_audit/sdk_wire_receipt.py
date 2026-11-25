"""Check actual SDK response objects and emit a receipt without response bodies."""

import hashlib
import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator


def validate(spec, rows):
    receipt = []
    for row in rows:
        path, method, status = row['path'], row['method'], row['status']
        if method not in {'get', 'post', 'delete', 'patch', 'put'} or not 200 <= status < 300:
            raise ValueError('Expected a successful HTTP operation')
        response = spec['paths'][path][method]['responses'][str(status)]
        schema = response['content']['application/json']['schema']
        validator = Draft202012Validator(dict(components=spec['components'], **schema))
        # Diagnostics must never contain a session's credential or private data.
        if not validator.is_valid(row['value']):
            raise ValueError(f'Invalid SDK response for {method.upper()} {path} ({status})')
        payload = json.dumps(row['value'], ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), allow_nan=False).encode()
        receipt.append(dict(method=method.upper(), path=path, status=status,
                            json_sha256=hashlib.sha256(payload).hexdigest()))
    return receipt


if __name__ == '__main__':
    data = Path(sys.argv[1]).read_bytes()
    rows = validate(json.loads(data), json.load(sys.stdin))
    print(json.dumps(dict(spec_sha256=hashlib.sha256(data).hexdigest(), operations=rows)))

"""Capture missing-credential responses from an isolated native q4t ingress."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re

from tools.serve_recovery import test_native_ingress as native


CATALOG = Path(__file__).resolve().parents[2] / 'docs/evidence/serve-sdk-routes-20261126/methods.json'


def capture(binary, output):
    catalog = json.loads(CATALOG.read_bytes())
    routes = [(verb, path) for verb, path, *_ in catalog['rows']]
    routes.append(('POST', catalog['legacy_cancel_path']))
    output.mkdir(mode=0o700, parents=True)
    os.environ['Q4T_TEST_OUTPUT_DIR'] = str(output/'fixture')
    native.INGRESS = binary
    native.NativeTests.setUpClass()
    packets = {}
    try:
        case = native.NativeTests()
        case.setUp()
        try:
            case.ready()
            stack = case.stack
            before = len(stack.requests)
            for verb, template in routes:
                path = '/v1' + re.sub(r'\{[^}]+\}', 'q4tfixture', template)
                key = verb + ' ' + path
                if key in packets:
                    raise ValueError('duplicate SDK fixture target ' + key)
                status, headers, body = stack.request(key=None, method=verb, path=path)
                wanted = {'content-type', 'www-authenticate', 'x-request-id'}
                fields = {k.lower(): v for k, v in headers.items() if k.lower() in wanted}
                packets[key] = dict(status=status, headers=fields, body=body)
            submissions = len(stack.requests) - before
        finally:
            case.tearDown()
    finally:
        native.NativeTests.tearDownClass()
    receipt = dict(ingress_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
                   method_catalog_sha256=hashlib.sha256(CATALOG.read_bytes()).hexdigest(),
                   model_starts=0, backend_submissions=submissions, packets=packets)
    (output/'capture.json').write_text(json.dumps(receipt, indent=2) + '\n')
    if submissions or any(
        p['status'] != 401 or p['headers'].get('www-authenticate') != 'Bearer realm="q4t"'
        or p['body'].get('error', {}).get('code') != 'gateway_unauthorized'
        or not p['headers'].get('x-request-id') for p in packets.values()
    ):
        raise ValueError('unexpected authentication response; see capture.json')
    if len({p['headers']['x-request-id'] for p in packets.values()}) != len(packets):
        raise ValueError('request trace was reused across distinct requests')
    print(json.dumps(dict(captured=len(packets), model_starts=0, backend_submissions=0)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    capture(args.native.resolve(strict=True), args.output)

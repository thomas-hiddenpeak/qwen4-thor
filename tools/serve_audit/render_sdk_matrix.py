"""Join per-method public SDK measurements with the frozen G00 resource catalogue."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import re


CATALOG = Path(__file__).resolve().parents[2] / 'docs/evidence/serve-sdk-routes-20261126/methods.json'
VERSIONS = [('python', v) for v in ('2.8.1', '2.33.0', '3.42.1', '3.55.1')] + [
    ('node', v) for v in ('6.34.0', '7.47.0')]
MODES = ('resource', 'legacy_cancel', 'generic')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def render(capture, actors):
    catalog = json.loads(CATALOG.read_bytes())
    receipt = json.loads(capture.read_bytes())
    routes = {(verb, path): [py, js] for verb, path, py, js, _ in catalog['rows']}
    if len(routes) != len(catalog['rows']):
        raise ValueError('duplicate catalogue entry')
    expected = set(routes) | {('POST', catalog['legacy_cancel_path'])}
    keys = {verb + ' /v1' + re.sub(r'\{[^}]+\}', 'q4tfixture', path)
            for verb, path in expected}
    if (set(receipt['packets']) != keys or receipt['backend_submissions'] != 0
            or receipt['method_catalog_sha256'] != sha(CATALOG)):
        raise ValueError('capture does not cover every distinct catalogue route')
    for packet in receipt['packets'].values():
        if (packet['status'] != 401 or not packet['headers'].get('x-request-id')
                or packet['headers'].get('www-authenticate') != 'Bearer realm="q4t"'
                or packet['body'].get('error', {}).get('code') != 'gateway_unauthorized'):
            raise ValueError('incomplete gateway challenge')
    output = io.StringIO(newline='')
    table = csv.writer(output, lineterminator='\n')
    table.writerow(['method', 'path', 'python_accessor', 'node_accessor',
                    *(lang + '-' + version for lang, version in VERSIONS)])
    capture_hash = sha(capture)
    totals = {}
    for lang, version in VERSIONS:
        label = lang + '-' + version
        source = actors/(label + '.json')
        measured = json.loads(source.read_bytes())
        if (measured['language'], measured['version']) != (lang, version):
            raise ValueError('unexpected SDK identity: ' + label)
        if measured['capture_sha256'] != capture_hash:
            raise ValueError('SDK observed another capture: ' + label)
        rows = {(verb, path): mode for verb, path, mode in measured['rows']}
        if rows.keys() != routes.keys() or len(rows) != len(measured['rows']):
            raise ValueError('incomplete or duplicate SDK route: ' + label)
        for key, mode in rows.items():
            if mode not in MODES or (mode == 'legacy_cancel' and
                    key != ('POST', '/evals/{eval_id}/runs/{run_id}/cancel')):
                raise ValueError('unknown SDK invocation: ' + str((key, mode)))
            routes[key].append(mode)
        totals[label] = dict(measured_sha256=sha(source),
                             **{mode: list(rows.values()).count(mode) for mode in MODES})
    for (verb, path), methods in routes.items():
        table.writerow([verb, path, *methods])
    return output.getvalue(), dict(catalog_sha256=sha(CATALOG), capture_sha256=capture_hash,
        ingress_sha256=receipt['ingress_sha256'], methods=len(routes), wire_targets=len(keys),
        sdk_invocations=len(routes)*len(VERSIONS), sdk_versions=totals, model_starts=receipt['model_starts'],
        backend_submissions=receipt['backend_submissions'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('capture', type=Path)
    parser.add_argument('actors', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    matrix, receipt = render(args.capture, args.actors)
    args.output.mkdir(exist_ok=True, parents=True)
    table = args.output/'methods.csv'
    table.write_bytes(matrix.encode())
    receipt['csv_sha256'] = sha(table)
    (args.output/'matrix.json').write_text(json.dumps(receipt, indent=2) + '\n')

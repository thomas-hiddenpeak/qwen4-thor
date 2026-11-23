"""Compare the frozen G00 scope with operations in an issued local OpenAPI."""

import hashlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qsl

ROOT = Path(__file__).resolve().parents[2]
SCOPE = ROOT / 'docs/evidence/serve-scope-decision-20261120/endpoints.json'
ISSUED = ROOT / 'docs/serve-openapi-v1.json'
METHODS = {'get', 'post', 'put', 'patch', 'delete', 'options', 'head', 'trace'}


def template(path):
    return re.sub(r'\{[^/{}]+\}', '{}', path)


def local_ref(document, value):
    visited = set()
    while '$ref' in value:
        ref = value['$ref']
        if not ref.startswith('#/') or ref in visited:
            raise ValueError(f'Expected an acyclic local parameter reference: {ref}')
        visited.add(ref)
        value = document
        for key in ref[2:].split('/'):
            value = value[key.replace('~1', '/').replace('~0', '~')]
    return value


def includes_selector(document, shared, operation, selectors):
    if not selectors:
        return True
    schemas = {}
    for parameter in [*shared, *operation.get('parameters', [])]:
        param = local_ref(document, parameter)
        if param['in'] == 'query':
            schemas[param['name']] = param.get('schema', {})
    for key, value in selectors:
        schema = local_ref(document, schemas.get(key, {}))
        if schema.get('const') != value and value not in schema.get('enum', []):
            return False
    return True


def compare(scope, document):
    paths = {}
    for path, operations in document['paths'].items():
        if path.startswith('/v1/'):
            key = template(path[3:])
            if key in paths:
                raise ValueError(f'Ambiguous path template: {path}')
            paths[key] = (path, operations)
    rows = []
    for row in scope['endpoints']:
        if row['scope'] != 'required_inference_or_application_service':
            continue
        base, _, query = row['path'].partition('?')
        selectors = parse_qsl(query, strict_parsing=True, keep_blank_values=True)
        path, item = paths.get(template(base), (None, {}))
        methods = sorted(method.upper() for method, operation in item.items()
                         if method in METHODS and includes_selector(
                             document, item.get('parameters', []), operation, selectors))
        rows.append(dict(path=row['path'], tasks=row['tasks'],
                         issued_path=path if methods else None, issued_methods=methods))
    return rows


def report(scope_path, issued_path):
    scope_data, issued_data = scope_path.read_bytes(), issued_path.read_bytes()
    rows = compare(json.loads(scope_data), json.loads(issued_data))
    declared = sum(bool(row['issued_methods']) for row in rows)
    return dict(scope_snapshot_sha256=hashlib.sha256(scope_data).hexdigest(),
                issued_openapi_sha256=hashlib.sha256(issued_data).hexdigest(),
                scope_path_selectors=len(rows), with_declared_operations=declared,
                without_declared_operations=len(rows) - declared,
                measurement='Path templates ignore parameter names. A query selector must be explicitly declared with a matching enum or constant on the listed operation. This measures declarations only; the frozen scope inventory does not list all required methods or their acceptance.',
                rows=rows)


if __name__ == '__main__':
    json.dump(report(SCOPE, ISSUED), sys.stdout, indent=2, ensure_ascii=False)
    print()

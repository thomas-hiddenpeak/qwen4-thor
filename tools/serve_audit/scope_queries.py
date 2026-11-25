"""Compare query names and optional explicit defaults on declared in-scope methods."""
import argparse
import hashlib
import json
from pathlib import Path

from . import scope_methods, scope_routes


def parameters(document, path, method):
    route = document['paths'][path]
    fields = {}
    for group in (route.get('parameters', []), route[method].get('parameters', [])):
        current = {}
        for value in group:
            param = scope_routes.local_ref(document, value)
            if param['in'] == 'query':
                name = param['name']
                if name in current:
                    raise ValueError(f'Duplicate query parameter on {method} {path}: {name}')
                current[name] = param
        fields.update(current)
    return fields


def compare(scope, official, issued):
    routes = {r['path']:r for r in scope_routes.compare(scope, issued)}
    methods = scope_methods.compare(scope, official, issued)
    rows = []
    for operation in methods['rows']:
        if not operation['declared']:
            continue
        path = operation['path']
        method = operation['method'].lower()
        declared_path = routes[path]['issued_path']
        current = parameters(issued, declared_path, method)
        for name, required in parameters(official, path, method).items():
            # Keep the wire spelling visible. Bracket arrays are an SDK
            # serialization convention; their presence alone cannot establish
            # that the server accepts the canonical repeated parameter.
            alias = name+'[]'
            is_array = scope_routes.local_ref(official, required.get('schema', {})).get('type') == 'array'
            state = 'declared' if name in current else 'missing'
            if state == 'missing' and is_array and alias in current:
                state = 'bracket_name_only'
            rows.append(dict(method=operation['method'], path=path, query=name, state=state))
    return dict(declared_methods=methods['declared'], required_methods=methods['required_methods'],
                official_query_parameters=len(rows),
                counts={state:sum(r['state']==state for r in rows)
                        for state in ('declared','bracket_name_only','missing')}, rows=rows)



def json_value_key(value):
    # JSON has a single number type and distinct boolean values.
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        return ('number', value)
    if isinstance(value, list):
        return ('array', tuple(json_value_key(x) for x in value))
    if isinstance(value, dict):
        return ('object', tuple((k, json_value_key(v)) for k, v in sorted(value.items())))
    return (type(value).__name__, value)


def schema_defaults(document, schema):
    """Collect unconditional JSON Schema default annotations on a parameter."""
    values = {}
    refs = set()
    pending = [schema]
    while pending:
        node = pending.pop()
        if not isinstance(node, dict):
            continue
        if 'default' in node:
            value = node['default']
            values[json_value_key(value)] = value
        pending.extend(node.get('allOf', []))
        ref = node.get('$ref')
        if ref is None or ref in refs:
            continue
        if not ref.startswith('#/'):
            raise ValueError(f'Use a bundled OpenAPI for schema defaults: {ref}')
        refs.add(ref)
        target = document
        for key in ref[2:].split('/'):
            target = target[key.replace('~1', '/').replace('~0', '~')]
        pending.append(target)
    return [values[key] for key in sorted(values)]


def compare_defaults(scope, official, issued):
    routes = {r['path']:r for r in scope_routes.compare(scope, issued)}
    rows = []
    name_report = compare(scope, official, issued)
    for parameter in name_report['rows']:
        path, method, name = parameter['path'], parameter['method'].lower(), parameter['query']
        upstream = parameters(official, path, method)[name]
        expected = schema_defaults(official, upstream.get('schema', {}))
        if not expected:
            continue
        current = parameters(issued, routes[path]['issued_path'], method)
        wire_name = name + '[]' if parameter['state'] == 'bracket_name_only' else name
        actual = schema_defaults(issued, current.get(wire_name, {}).get('schema', {}))
        if len(expected) != 1 or len(actual) > 1:
            state = 'ambiguous'
        elif not actual:
            state = 'missing'
        elif json_value_key(actual[0]) == json_value_key(expected[0]):
            state = 'matching'
        else:
            state = 'different'
        rows.append(dict(method=method.upper(), path=path, query=name,
                         official=expected, issued=actual, state=state))
    return dict(query_parameters=name_report['official_query_parameters'],
                explicit_defaults=len(rows), counts={state:sum(r['state'] == state for r in rows)
                    for state in ('matching', 'missing', 'different', 'ambiguous')}, rows=rows)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('official', type=Path)
    parser.add_argument('--issued', type=Path)
    parser.add_argument('--defaults', action='store_true',
                        help='Compare explicit unconditional defaults using bundled documents.')
    args = parser.parse_args()
    issued = args.issued or (scope_routes.ROOT / 'docs/serve-openapi-bundle-v1.json'
                             if args.defaults else scope_routes.ISSUED)
    files = dict(scope=scope_routes.SCOPE, official=args.official, issued=issued)
    data = {k:p.read_bytes() for k,p in files.items()}
    audit = compare_defaults if args.defaults else compare
    report = audit(**{k:json.loads(raw) for k,raw in data.items()})
    measurement = ('Explicit schema defaults on declared methods; execution requires separate verification.'
                   if args.defaults else 'Official query parameter names on declared methods. Types, defaults, '
                                         'serialization and execution require separate verification.')
    print(json.dumps(dict(sources_sha256={k:hashlib.sha256(raw).hexdigest() for k,raw in data.items()},
                          measurement=measurement,
                          **report), indent=2))


if __name__ == '__main__':
    main()

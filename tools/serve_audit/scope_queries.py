"""Compare query parameter names on each declared in-scope HTTP method."""
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('official', type=Path)
    parser.add_argument('--issued', type=Path, default=scope_routes.ISSUED)
    args = parser.parse_args()
    files = dict(scope=scope_routes.SCOPE, official=args.official, issued=args.issued)
    data = {k:p.read_bytes() for k,p in files.items()}
    report = compare(**{k:json.loads(raw) for k,raw in data.items()})
    print(json.dumps(dict(sources_sha256={k:hashlib.sha256(raw).hexdigest() for k,raw in data.items()},
                          measurement='Official query parameter names on declared methods. Types, defaults, '
                                      'serialization and execution require separate verification.',
                          **report), indent=2))


if __name__ == '__main__':
    main()

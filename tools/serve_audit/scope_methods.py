"""Compare the frozen inference/application methods against an issued OpenAPI."""
import argparse
import hashlib
import json
from pathlib import Path

from . import scope_routes


def success_codes(operation):
    return sorted(code for code in operation['responses']
                  if code.startswith('2') or code == '101')


def compare(scope, official, issued):
    paths = official['paths']
    if set(paths) != {row['path'] for row in scope['endpoints']}:
        raise ValueError('Official specification differs from the frozen catalogue')
    rows = []
    for row in scope_routes.compare(scope, issued):
        local = issued['paths'].get(row['issued_path'], {})
        for method, operation in paths[row['path']].items():
            if method not in scope_routes.METHODS:
                continue
            verb = method.upper()
            required = success_codes(operation)
            if not required:
                raise ValueError(f'Missing official success response for {verb} {row["path"]}')
            declared = verb in row['issued_methods']
            actual = success_codes(local[method]) if declared else []
            rows.append(dict(method=verb, path=row['path'], tasks=row['tasks'],
                             declared=declared, expected_success=required,
                             issued_success=actual))
    matches = sum(r['declared'] and r['expected_success'] == r['issued_success']
                  for r in rows)
    declared = sum(r['declared'] for r in rows)
    return dict(required_methods=len(rows), declared=declared,
                missing_declaration=len(rows)-declared,
                declared_with_different_success=declared-matches,
                exact_success_declarations=matches, rows=rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('official', type=Path)
    parser.add_argument('--issued', type=Path, default=scope_routes.ISSUED)
    args = parser.parse_args()
    raw = {key: path.read_bytes() for key, path in
           [('scope', scope_routes.SCOPE), ('official', args.official),
            ('issued', args.issued)]}
    audit = compare(*(json.loads(raw[key]) for key in ('scope', 'official', 'issued')))
    print(json.dumps(dict(sources_sha256={k: hashlib.sha256(v).hexdigest()
                                          for k, v in raw.items()},
                          measurement='Declared methods and HTTP success responses only. '
                                      'Runtime behavior requires independent evidence.',
                          **audit), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

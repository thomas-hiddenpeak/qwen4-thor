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
    excluded = []
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
            result = dict(method=verb, path=row['path'], tasks=row['tasks'],
                          declared=declared, expected_success=required,
                          issued_success=actual)
            if (verb, row['path']) == ('DELETE', '/models/{model}'):
                # GET serves inference clients on this same upstream path.
                # DELETE is the organization owner's fine-tuned model operation.
                if (operation.get('operationId') != 'deleteModel' or
                        operation.get('summary') != 'Delete a fine-tuned model' or
                        'Owner role in your organization' not in operation.get('description', '')):
                    raise ValueError('Review changed platform scope for DELETE /models/{model}')
                excluded.append(dict(**result, operation_id=operation['operationId'],
                                     scope='excluded_platform_management_by_user',
                                     reason=operation['description']))
            else:
                rows.append(result)
    matches = sum(r['declared'] and r['expected_success'] == r['issued_success']
                  for r in rows)
    declared = sum(r['declared'] for r in rows)
    return dict(methods_on_required_paths=len(rows) + len(excluded),
                excluded_methods_on_required_paths=excluded,
                required_methods=len(rows), declared=declared,
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

"""Extract template input sizes from the official OpenAPI at the frozen scope revision."""
import argparse
import hashlib
import json
from pathlib import Path

REVISION = '926a7b8133226630bac025350e84b0ca5c31d45a'
DIGEST = '718389d43b97973b8687134f17e5d4c502660538cb8207bf0617288f20819057'
OUTPUT = Path(__file__).with_name('environment_http_bounds.json')


def extract(document, root):
    """Paths name SDK object fields; array and union wrappers do not add a name."""
    rules = {}
    schemas = document['components']['schemas']
    def sizes(node):
        values = {k: node[k] for k in ('minLength', 'maxLength', 'maxItems',
                                       'maxProperties') if k in node}
        for key in ('items', 'additionalProperties', 'propertyNames'):
            sub = node.get(key)
            if isinstance(sub, dict):
                limits = sizes(sub)
                if limits:
                    values[key] = limits
        return values

    def visit(node, path):
        if '$ref' in node:
            return visit(schemas[node['$ref'].split('/')[-1]], path)
        for variant in [*node.get('oneOf', []), *node.get('anyOf', [])]:
            visit(variant, path)
        for name, field in node.get('properties', {}).items():
            child = (*path, name)
            limits = sizes(field)
            if limits:
                key = '.'.join(child)
                if rules.setdefault(key, limits) != limits:
                    raise ValueError(f'Variant-specific bounds need an explicit discriminator: {key}')
            visit(field, child)
        if 'items' in node:
            visit(node['items'], path)

    visit(schemas[root], ())
    return rules


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('spec', type=Path)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    raw = args.spec.read_bytes()
    if hashlib.sha256(raw).hexdigest() != DIGEST:
        raise ValueError('Review the frozen specification before changing the reference digest')
    schema = json.loads(raw)
    create = extract(schema, 'CreateEnvironmentTemplateParams')
    if create != extract(schema, 'UpdateEnvironmentTemplateParams'):
        raise ValueError('Creation and update require separate bounds')
    output = json.dumps(dict(source='https://github.com/openai/openai-openapi/blob/' +
                             REVISION + '/openapi.json', sha256=DIGEST, fields=create),
                        indent=2, sort_keys=True) + '\n'
    if args.check:
        if output != OUTPUT.read_text():
            raise ValueError('Environment bounds differ from the reference')
    else:
        OUTPUT.write_text(output)


if __name__ == '__main__':
    main()

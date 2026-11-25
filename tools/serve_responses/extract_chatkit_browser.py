"""Regenerate browser output declarations from the reviewed chatkit-python tree."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

REVISION = 'dacc133c280b39b9334d06ea73f0f1c199e59927'
PREFIX = 'chatkit_browser_output_'
DEST = (Path(__file__).resolve().parents[2] /
        'src/server/contracts/openai_chatkit_browser_output_v1.json')


def ref(name):
    return {'$ref': '#/$defs/' + PREFIX + name}


def wire(node):
    """Keep validation constraints and make discriminated tags mandatory on wire."""
    if isinstance(node, list):
        return [wire(item) for item in node]
    if not isinstance(node, dict):
        return node
    result = {}
    for key, value in node.items():
        if key == 'properties':
            result[key] = {field: wire(child) for field, child in value.items()}
        elif key == '$ref':
            assert value.startswith('#/$defs/')
            result.update(ref(value.removeprefix('#/$defs/')))
        elif key not in ('$defs', 'description', 'title', 'examples', 'deprecated',
                          'default', 'discriminator'):
            result[key] = wire(value)
    if 'discriminator' in node:
        tag = node['discriminator']['propertyName']
        result['required'] = sorted(set(result.get('required', [])) | {tag})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path, help='reviewed upstream source directory')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    source = args.source.resolve(strict=True)
    inputs = ['LICENSE', *('chatkit/' + name + '.py' for name in
                           ('types', 'widgets', 'actions', 'icons', 'errors'))]
    hashes = {name: hashlib.sha256((source / name).read_bytes()).hexdigest()
              for name in inputs}
    if DEST.exists() and hashes != json.loads(DEST.read_bytes())['sources_sha256']:
        parser.error('upstream sources differ from the reviewed snapshot')
    sys.path.insert(0, str(source))
    import pydantic
    from chatkit.types import Page, SyncCustomActionResponse, Thread, ThreadItem, ThreadStreamEvent
    from pydantic import TypeAdapter
    assert pydantic.__version__ == '2.13.4'
    definitions = {}
    for name, shape in [('event', ThreadStreamEvent), ('item', ThreadItem), ('thread', Thread),
                        ('threads_page', Page[Thread]), ('items_page', Page[ThreadItem]),
                        ('sync', SyncCustomActionResponse)]:
        schema = TypeAdapter(shape).json_schema(mode='serialization')
        for key, part in schema.get('$defs', {}).items():
            value = wire(part)
            if definitions.setdefault(PREFIX + key, value) != value:
                raise ValueError('conflicting declaration for ' + key)
        root = wire(schema)
        if name in ('threads_page', 'items_page'):
            root['required'] = ['data', 'has_more', 'after']
        elif name == 'sync':
            root['required'] = ['updated_item']
        definitions[PREFIX + name] = root
    graph = dict(repository='openai/chatkit-python', revision=REVISION, sources_sha256=hashes,
                 pydantic=pydantic.__version__, definitions=definitions)
    output = json.dumps(graph, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n'
    if args.check:
        if DEST.read_text() != output:
            parser.error('browser output schema drift')
    else:
        DEST.write_text(output)


if __name__ == '__main__':
    main()

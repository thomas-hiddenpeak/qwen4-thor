"""Check persistent video bindings for the uploaded, inline and continued Responses."""
import base64
import hashlib
import json
from pathlib import Path
import re

from tools.serve_service import package


def check(directory, sdk, snapshots):
    owner = hashlib.sha256(b'client:alpha').hexdigest()
    uploaded, inline = sdk['sources']
    package.require(uploaded != inline and set(snapshots) == {uploaded, inline}
                    and len(set(snapshots.values())) == 2, 'expected two distinct video inputs')
    expected = {'uploaded': uploaded, 'inline': inline, 'replay': uploaded}

    def read(kind, ident):
        package.require(isinstance(ident, str) and re.fullmatch(r'[A-Za-z0-9_-]+', ident),
                        'invalid fixture object ID')
        path = Path(directory) / f'{owner}.{kind}.{ident}.json'
        record = package.read(path)['record']
        package.require(record['owner'] == owner and record['id'] == ident and record['kind'] == kind,
                        'stored video object differs from its expected kind, ID or owner')
        return record['data']

    rows = {}
    for label, source in expected.items():
        rid = sdk['responses'][label]['response']
        data = read('response', rid)
        binding, = data['input_snapshots']
        ref = binding['snapshot']
        package.require(binding['path'] == '/content/0' and ref['id'] == snapshots[source]
                        and data['response']['id'] == rid, 'response refers to a different input')
        snapshot = read('input_snapshot', ref['id'])
        raw = json.dumps(snapshot, ensure_ascii=False, separators=(',', ':')).encode()
        package.require(hashlib.sha256(raw).hexdigest() == ref['sha256']
                        and snapshot['binding_sha256'] == binding['binding_sha256'],
                        'stored video reference or input binding changed')
        item, = (item for item in data['history'] if item['id'] == binding['item_id'])
        part = item['content'][0]
        own = {item['id'] for item in data['own_input']}
        parent = sdk['saved']['response'] if label == 'replay' else None
        package.require(part['type'] == 'input_file' and part['detail'] == 'high'
                        and (binding['item_id'] in own) == (label != 'replay')
                        and data['response'].get('previous_response_id') == parent,
                        'file input was not retained in the expected turn')
        if label == 'inline':
            mime, comma, encoded = part['file_data'].partition(',')
            package.require(comma and mime == 'data:video/quicktime;base64'
                            and hashlib.sha256(base64.b64decode(encoded, validate=True)).hexdigest() == source,
                            'inline bytes differ from the stored video')
        else:
            package.require(part['file_id'] == sdk['saved']['file'],
                            'uploaded input no longer refers to the original file')
        rows[label] = dict(response=rid, item=binding['item_id'], snapshot=ref['id'],
                           snapshot_sha256=ref['sha256'], binding_sha256=binding['binding_sha256'])
    package.require(rows['uploaded']['item'] == rows['replay']['item']
                    and rows['uploaded']['item'] != rows['inline']['item'],
                    'continuation and inline video did not retain their own identities')
    return rows

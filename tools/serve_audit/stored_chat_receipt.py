"""Verify an SDK handoff from the source publication to the deleted Chat ID."""
import json
from urllib.parse import parse_qsl


def verify(writer, reader, request, metadata, source):
    def require(condition, reason):
        if not condition:
            raise ValueError(reason)

    expected = str(json.loads(source)['publication_revision'])
    messages = request['messages']
    require(source.decode() in messages[-1]['content'], 'original publication missing from Chat')
    require(writer['sdk'] != reader['sdk'], 'reader must use a different SDK')
    created = writer['created']
    identity = created['id']
    require(isinstance(identity, str) and identity.startswith('chatcmpl'), 'invalid stored completion ID')
    require(created['answer'].strip() == expected and created['metadata'] == request['metadata'],
            'wrong project answer or initial metadata')
    require(reader['before'] == created, 'reader fetched a different completion')
    revised = dict(created, metadata=metadata)
    require(reader['updated'] == reader['saved'] == revised, 'metadata or generated content changed')
    require(reader['listed'] == [identity] and reader['list_has_more'] is False, 'filtered list differs')
    require(reader['deleted'] == dict(id=identity, deleted=True), 'wrong deletion receipt')
    for case in ('forbidden', 'gone'):
        require(reader[case]['status'] == 404 and bool(reader[case]['code']), 'missing native 404')
    first, second = reader['pages']
    require(first['has_more'] is True and second['has_more'] is False and first['id'] != second['id'],
            'stored input page cursors differ')
    for page, original in zip(reader['pages'], messages, strict=True):
        require(page['id'] and page['content'] == original['content'] and page['role'] == original['role'],
                'original input message changed')
    collection = '/v1/chat/completions'
    target = collection + '/' + identity
    listing = [('limit', '1')] + [(f'metadata[{k}]', v) for k, v in metadata.items()]
    page = [('order', 'asc'), ('limit', '1')]
    expected_calls = [
        ('POST', collection, 200, []), ('GET', target, 200, []), ('GET', target, 404, []),
        ('POST', target, 200, []), ('GET', collection, 200, listing),
        ('GET', target+'/messages', 200, page),
        ('GET', target+'/messages', 200, page+[('after', first['id'])]),
        ('GET', target, 200, []), ('DELETE', target, 200, []), ('GET', target, 404, []),
    ]
    require(len(writer['requests']) == 1 and len(reader['requests']) == 9, 'unexpected retries or missing requests')
    ids = set()
    for wire, (verb, path, status, query) in zip(writer['requests']+reader['requests'], expected_calls, strict=True):
        require((wire['method'], wire['path'], wire['status']) == (verb, path, status), 'wire route or status differs')
        values = parse_qsl(wire['query'], strict_parsing=True, keep_blank_values=True)
        require(sorted(values) == sorted(query), 'wire query differs')
        trace = wire['trace']
        require(isinstance(trace, str) and trace and trace not in ids, 'missing or repeated request ID')
        ids.add(trace)
    return ids

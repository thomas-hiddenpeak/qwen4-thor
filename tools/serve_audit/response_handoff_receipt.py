"""Check durable Responses against the original project document and live stream."""
import json
from urllib.parse import parse_qsl


def verify(writer, reader, request, followup, source):
    def require(condition, message):
        if not condition:
            raise ValueError(message)

    published = json.loads(source)
    require(source.decode() in request['input'][-1]['content'][0]['text'], 'missing source receipt')
    require(writer['sdk'] != reader['sdk'], 'handoff needs two SDKs')
    require(type(writer['count']) is int and writer['count'] > 0, 'invalid token count')
    original = writer['created']
    ident = original['id']
    require(ident.startswith('resp_') and original['previous'] is None and original['status'] == 'completed'
            and original['metadata'] == request['metadata'], 'wrong first response')
    require(original['text'].strip() == str(published['publication_revision']), 'wrong publication revision')
    events = writer['events']
    require(events and [e['seq'] for e in events] == list(range(len(events))), 'event sequence has a gap')
    require(events[0]['type'] == 'response.created' and events[-1]['type'] == 'response.completed'
            and events[-1]['response'] == original, 'stream terminal differs')
    require(sum(e['type'] in ('response.failed', 'response.incomplete', 'response.completed') for e in events) == 1
            and ''.join(e['delta'] for e in events if e['type'] == 'response.output_text.delta') == original['text'],
            'live text or terminal count differs')
    require(reader['before'] == original and reader['events'] == events, 'persisted response or replay differs')
    follow = reader['follow']
    require(follow['id'].startswith('resp_') and follow['id'] != ident and follow['previous'] == ident
            and follow['metadata'] == followup['metadata'] and follow['status'] == 'completed'
            and follow['text'].strip() == str(published['version_revision']), 'wrong continuation or answer')
    for denial in (reader['foreign'], reader['gone']):
        require(denial['status'] == 404 and bool(denial['code']), 'missing native 404')
    first, second = reader['pages']
    require(first['id'] and second['id'] and first['id'] != second['id']
            and first['more'] is True and second['more'] is False, 'bad input page cursor')
    for page, item in zip(reader['pages'], request['input'], strict=True):
        require(page['role'] == item['role'] and page['text'] == [p['text'] for p in item['content']],
                'stored input body changed')

    post = '/v1/responses'
    saved = post+'/'+ident
    items = saved+'/input_items'
    page = [('order', 'asc'), ('limit', '1')]
    calls = [(writer, [('POST', post+'/input_tokens', 200, []), ('POST', post, 200, [])]),
             (reader, [('GET', saved, 200, []), ('GET', saved, 404, []), ('GET', items, 200, page),
                       ('GET', items, 200, page+[('after', first['id'])]), ('POST', post, 200, []),
                       ('GET', saved, 200, [('stream', 'true')]), ('DELETE', saved, 200, []),
                       ('GET', saved, 404, [])])]
    traces = set()
    for actor, expected in calls:
        selector = [('beta', 'true')] if actor['beta'] else []
        for packet, (method, path, status, query) in zip(actor['requests'], expected, strict=True):
            require((packet['method'], packet['path'], packet['status']) == (method, path, status),
                    'wrong wire method, path or status')
            observed = parse_qsl(packet['query'], strict_parsing=True, keep_blank_values=True)
            require(sorted(observed) == sorted(query+selector), 'wire selector or pagination differs')
            trace = packet['trace']
            require(isinstance(trace, str) and bool(trace) and trace not in traces, 'missing or repeated trace')
            traces.add(trace)
    return traces

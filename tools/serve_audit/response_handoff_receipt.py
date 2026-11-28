"""Check durable Responses against the original project document and live stream."""
import json
from urllib.parse import parse_qsl


def require(condition, message):
    if not condition:
        raise ValueError(message)


def verify_text_lifecycle(events, original):
    """Reconstruct the messages and their text parts from the ordered events."""
    messages = {}
    parts = {}
    closed = set()
    text_done = set()
    part_done = set()
    for event in events:
        kind = event['type']
        if kind in ('response.created', 'response.in_progress', 'response.completed'):
            require(event['response_id'] == original['id'], 'wrong response identity')
        if kind in ('response.output_item.added', 'response.output_item.done'):
            if event['item_type'] != 'message':
                continue
            index, item = event['output_index'], event['item_id']
            if kind == 'response.output_item.added':
                require(type(index) is int and index >= 0 and index not in messages
                        and item and item not in messages.values(), 'duplicate or invalid message identity')
                require(event['content'] == [], 'new message already contains text parts')
                messages[index] = item
                parts[index] = []
            else:
                require(index in messages and messages[index] == item and index not in closed,
                        'wrong completed message identity')
                require(all((index, j) in part_done for j in range(len(parts[index])))
                        and event['content'] == parts[index], 'completed message parts differ')
                closed.add(index)
            continue
        if kind not in ('response.content_part.added', 'response.content_part.done',
                        'response.output_text.delta', 'response.output_text.done'):
            continue
        index, part_index = event['output_index'], event['content_index']
        require(index in messages and messages[index] == event['item_id'] and index not in closed,
                'content belongs to the wrong message')
        require(type(part_index) is int and part_index >= 0, 'invalid content index')
        key = (index, part_index)
        content = parts[index]
        if kind == 'response.content_part.added':
            part = event['part']
            require(part_index == len(content) and part['type'] == 'output_text'
                    and part['text'] == '', 'wrong new text part')
            content.append(dict(part))
            continue
        require(part_index < len(content) and key not in part_done, 'text part is missing or closed')
        part = content[part_index]
        if kind == 'response.output_text.delta':
            require(key not in text_done, 'delta follows done text')
            part['text'] += event['delta']
        elif kind == 'response.output_text.done':
            require(key not in text_done and part['text'] == event['text'], 'done text differs from its deltas')
            text_done.add(key)
        else:
            final = event['part']
            require(key in text_done and final['type'] == part['type'] and final['text'] == part['text'],
                    'completed part differs from done text')
            content[part_index] = final
            part_done.add(key)
    require(part_done and closed == messages.keys() and
            original['messages'] == [messages[i] for i in sorted(messages)] and
            original['message_parts'] == {str(i): content for i, content in parts.items()},
            'terminal message identity or content differs')
    require(''.join(part['text'] for i in sorted(parts) for part in parts[i]) == original['text'],
            'terminal response text differs from its parts')


def verify(writer, reader, request, followup, source):
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
    require(sum(e['type'] in ('response.failed', 'response.incomplete', 'response.completed') for e in events) == 1,
            'stream terminal count differs')
    verify_text_lifecycle(events, original)
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

"""Verify the five native HTTP exchanges of a Node SDK project-release query."""
import copy
import json
from urllib.parse import parse_qs, urlsplit

from tools.serve_responses.sdk_capture import require, wire_events


def current_event(event, item, index):
    """Convert only the two SDK presentation fields, checking their context."""
    projected = copy.deepcopy(event)
    if event['type'] == 'response.function_call_arguments.done':
        require((event.get('item_id'), event.get('output_index'),
                 event.get('name'), event.get('arguments')) ==
                (item['id'], index, item['name'], item['arguments']),
                'argument completion belongs to another function or item')
        del projected['name']
    response = projected.get('response')
    if response is not None:
        require(response['object'] == 'response' and
                response['prompt_cache_retention'] == 'in-memory',
                'wrong legacy response envelope')
        response['prompt_cache_retention'] = 'in_memory'
    return projected


def check(packets, sdk, revision):
    shape = {'live': ('legacy', 'POST', 200, 'text/event-stream'),
             'retained': ('legacy', 'GET', 200, 'text/event-stream'),
             'current': ('current', 'GET', 200, 'text/event-stream'),
             'answer': ('legacy', 'POST', 200, 'application/json'),
             'foreign': ('foreign', 'GET', 404, 'application/json')}
    require(packets.keys() == shape.keys(), 'missing or extra HTTP exchange')
    for name, (reader, method, status, mime) in shape.items():
        packet = packets[name]
        require((packet['reader'], packet['method'], packet['status'],
                 packet['type'].partition(';')[0]) ==
                (reader, method, status, mime), 'unexpected HTTP envelope')
        url = urlsplit(packet['path'])
        require(not url.netloc and not url.scheme and not url.fragment,
                'unexpected request target')
        target = '/v1/responses'
        params = {}
        if method == 'GET':
            target += '/' + sdk['response_id']
        if name in ('current', 'retained'):
            params = {'starting_after': [str(sdk['cursor'])],
                      'stream': ['true'], 'include_obfuscation': ['false']}
        require(url.path == target and
                parse_qs(url.query, keep_blank_values=True) == params,
                'request route or resume cursor differs')

    full = wire_events(packets['live']['body'])
    retained, current = (wire_events(packets[k]['body'], after=sdk['cursor'])
                         for k in ('retained', 'current'))
    require([e['sequence_number'] for e in full] == list(range(len(full))) and
            [e['type'] for e in full].count('response.completed') == 1 and
            full[-1]['type'] == 'response.completed', 'invalid journal')
    response = full[-1]['response']
    require(response['id'] == sdk['response_id'] and response['status'] == 'completed'
            and response['error'] is None and response['incomplete_details'] is None,
            'wrong original response or terminal status')
    calls = [(i, it) for i, it in enumerate(response['output'])
             if it['type'] == 'function_call']
    require(len(calls) == 1, 'expected one issued function call')
    index, item = calls[0]
    require((item['id'], item['call_id'], item['name']) ==
            (sdk['item_id'], sdk['call_id'], 'get_project_release') and
            json.loads(item['arguments']) == {}, 'wrong tool identity')
    markers = [e for e in full if e['type'] == 'response.function_call_arguments.done']
    require(len(markers) == 1 and markers[0] == sdk['marker'] and
            sdk['cursor'] == markers[0]['sequence_number'] - 1 and
            retained == full[sdk['cursor'] + 1:] and retained[0] == markers[0],
            'legacy journal changed on resume')
    require(current == [current_event(e, item, index) for e in retained],
            'current reader changed a retained event')

    denied = json.loads(packets['foreign']['body'])
    require(denied['error']['code'] == 'object_not_found' and
            sdk['response_id'] not in packets['foreign']['body'], 'wrong owner error')
    create = packets['live']['input']
    require(create['stream'] is True and create['store'] is True and
            create['tool_choice'] == {'type': 'function', 'name': item['name']} and
            [t['name'] for t in create['tools']] == [item['name']],
            'unexpected tool creation request')
    followup = packets['answer']['input']
    outputs = followup['input']
    require(followup['previous_response_id'] == response['id'] and
            followup['tool_choice'] == 'none' and len(outputs) == 1 and
            outputs[0]['type'] == 'function_call_output' and
            outputs[0]['call_id'] == item['call_id'] and
            json.loads(outputs[0]['output']) == {'revision': revision},
            'wrong tool result or parent')
    answer = json.loads(packets['answer']['body'])
    messages = [it for it in answer['output'] if it['type'] == 'message']
    require(answer['object'] == 'response' and answer['status'] == 'completed' and
            answer['error'] is None and answer['incomplete_details'] is None and
            messages and all(m['status'] == 'completed' for m in messages) and
            answer['previous_response_id'] == response['id'] and
            answer['id'] == sdk['followup'] and answer['id'] != response['id'] and
            ''.join(p['text'] for msg in messages for p in msg['content']
                    if p['type'] == 'output_text').strip() == str(revision),
            'fresh reply did not return the observed release')
    return full, current, answer

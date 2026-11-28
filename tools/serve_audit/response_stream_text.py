"""Observe the installed OpenAI Python SDK's high-level Responses stream."""
import json
import ssl
import sys
from urllib.parse import urlsplit

import openai


def observe(plan):
    base = urlsplit(plan['base'])
    if (base.scheme, base.hostname, base.path) != ('https', '127.0.0.1', '/v1'):
        raise ValueError('expected an isolated local TLS fixture')
    ctx = ssl.create_default_context(cafile=plan['ca'])
    requests = []
    def record(reply):
        url = reply.request.url
        requests.append(dict(method=reply.request.method, path=url.path, query=url.query.decode(),
                             status=reply.status_code, trace=reply.headers.get('x-request-id')))
    http = openai.DefaultHttpxClient(verify=ctx, trust_env=False, event_hooks={'response': [record]})
    request = dict(plan['request'])
    events, annotations = [], []
    with openai.OpenAI(api_key=plan['key'], base_url=plan['base'],
                       http_client=http, timeout=90, max_retries=0) as client:
        with client.responses.stream(**request) as stream:
            for event in stream:
                events.append([event.sequence_number, event.type])
                if event.type == 'response.output_text.annotation.added':
                    annotations.append({
                        'item_id': event.item_id, 'output_index': event.output_index,
                        'content_index': event.content_index, 'annotation_index': event.annotation_index,
                        'annotation': event.annotation,
                    })
            response = stream.get_final_response()
    # SDK versions model this particular event's annotation as either a JSON
    # dictionary or a typed object. Observe both through their public API.
    for entry in annotations:
        value = entry['annotation']
        if hasattr(value, 'model_dump'):
            entry['annotation'] = value.model_dump(mode='json', exclude_unset=True)
    messages = [[item.id, part.text,
                 [a.model_dump(mode='json', exclude_unset=True) for a in part.annotations]]
                for item in response.output if item.type == 'message'
                for part in item.content if part.type == 'output_text']
    searches = [[item.id, item.status, item.action.model_dump(mode='json', exclude_unset=True)]
                for item in response.output if item.type == 'web_search_call']
    return dict(sdk='python-'+openai.__version__, id=response.id, status=response.status,
                text=response.output_text, messages=messages, searches=searches,
                events=events, annotations=annotations, requests=requests)


if __name__ == '__main__':
    print(json.dumps(observe(json.load(sys.stdin)), ensure_ascii=False))

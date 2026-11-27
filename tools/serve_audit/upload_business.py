"""One stock Python SDK call in the managed multipart handoff."""
import base64
import hashlib
import json
import sys
from urllib.parse import urlsplit

import openai


def execute(client, request):
    action = request['action']
    body = request.get('body', {})
    identity = request.get('id')
    if action == 'upload':
        return client.uploads.create(**body)
    if action == 'part':
        return client.uploads.parts.create(identity, data=('part.bin', base64.b64decode(body['data'])))
    if action == 'complete':
        return client.uploads.complete(identity, **body)
    if action == 'file':
        return client.files.create(file=(body['filename'], base64.b64decode(body['data']), 'text/plain'),
                                   purpose='user_data')
    if action == 'retrieve':
        return client.files.retrieve(identity)
    if action == 'download':
        content = client.files.content(identity).read()
        return {'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
    if action == 'list':
        page = client.files.list(purpose='user_data', limit=50)
        return {'ids': [file.id for file in page.data], 'has_more': page.has_more}
    if action == 'delete':
        return client.files.delete(identity)
    if action == 'answer':
        response = client.responses.create(**body)
        return {'id': response.id, 'status': response.status, 'answer': response.output_text}
    raise ValueError('unknown upload handoff operation')


def run(request):
    assert urlsplit(request['base']).hostname == '127.0.0.1'
    assert openai.__version__ == request['version']
    wire = []

    def observe(reply):
        received = reply.request
        wire.append({'method': received.method, 'path': received.url.path,
                     'status': reply.status_code, 'trace': reply.headers.get('x-request-id')})

    with openai.DefaultHttpxClient(trust_env=False, event_hooks={'response': [observe]}) as transport:
        client = openai.OpenAI(api_key=request['key'], base_url=request['base'], http_client=transport,
                               max_retries=0, timeout=65)
        try:
            data = execute(client, request)
            if not isinstance(data, dict):
                data = data.model_dump(exclude_unset=True)
        except openai.APIStatusError as error:
            data = {'error': error.code, 'status': error.status_code}
    assert len(wire) == 1, 'SDK retried or did not use HTTP'
    return {'sdk': 'python-' + openai.__version__, 'wire': wire[0], 'data': data}


if __name__ == '__main__':
    print(json.dumps(run(json.load(sys.stdin))))

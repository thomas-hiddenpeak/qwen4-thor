"""Use the official Python clients to exercise scoped Assistants v2 storage."""
from contextlib import ExitStack
import json
import ssl
import sys
import time
from urllib.parse import urlsplit

import openai


def run(config):
    origin = urlsplit(config['base'])
    if origin.scheme != 'https' or origin.hostname != '127.0.0.1':
        raise ValueError('Expected a local native TLS endpoint')
    requests = []

    def observe(reply):
        request = reply.request
        assert request.url.host == origin.hostname and request.url.port == origin.port
        reply.read()
        requests.append(dict(
            method=request.method, path=request.url.path, query=request.url.query.decode(),
            trace=reply.headers.get('x-request-id'), beta=request.headers.get('openai-beta'),
            status=reply.status_code, body=json.loads(request.content) if request.content else None,
            response=reply.json()))
        time.sleep(.13)

    ids = {}
    tls = ssl.create_default_context(cafile=config['ca'])
    with ExitStack() as stack:
        clients = {scope: stack.enter_context(openai.OpenAI(
            api_key=config[key], base_url=config['base'], max_retries=0, timeout=20,
            http_client=openai.DefaultHttpxClient(
                verify=tls, trust_env=False, follow_redirects=False, event_hooks={'response':[observe]})))
            for scope, key in [('owner','key'), ('other','other')]}
        for step in config['steps']:
            before = len(requests)
            group, op = step['call'].split('.')
            beta = clients[step['scope']].beta
            api = {'assistants': beta.assistants, 'threads': beta.threads,
                   'messages': beta.threads.messages}[group]
            args = [ids[key] for key in step['args']]
            fields = dict(step['fields'])
            if step.get('parent'):
                fields['thread_id'] = ids['thread']
            try:
                row = getattr(api, op)(*args, **fields)
                assert step['status'] == 200
                if step.get('save'):
                    ids[step['save']] = row.id
                item = (dict(object='list', ids=[v.id for v in row.data])
                        if op == 'list' else row.model_dump(mode='json', exclude_unset=True))
            except openai.APIStatusError as error:
                assert step['status'] == error.status_code == 404
                item = {'status': error.status_code}
            assert len(requests) == before + 1, step['name']
            requests[-1].update(step=step['name'], scope=step['scope'], sdk_value=item)
    return dict(sdk='python-'+openai.__version__, ids=ids, requests=requests)


if __name__ == '__main__':
    print(json.dumps(run(json.load(sys.stdin)), ensure_ascii=False))

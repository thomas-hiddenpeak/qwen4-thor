"""Exercise a nullable name update through the official vector store client."""
import json
import ssl
import sys
import time
from urllib.parse import urlsplit

import openai


def run(config):
    origin = urlsplit(config['base'])
    assert origin.scheme == 'https' and origin.hostname == '127.0.0.1'
    store_id = config['store']
    requests = []

    def observe(reply):
        request = reply.request
        assert request.url.host == origin.hostname and request.url.port == origin.port
        reply.read()
        payload = reply.json()
        rows = payload['data'] if 'data' in payload else [payload]
        matching = [row for row in rows if row['id'] == store_id]
        assert len(matching) == 1 and 'name' in matching[0]
        requests.append(dict(method=request.method, path=request.url.path,
                             query=request.url.query.decode(), status=reply.status_code,
                             trace=reply.headers.get('x-request-id'),
                             body=json.loads(request.content) if request.content else None,
                             name=matching[0]['name']))
        time.sleep(.125)

    ctx = ssl.create_default_context(cafile=config['ca'])
    with openai.OpenAI(base_url=config['base'], api_key=config['key'], max_retries=0,
                       timeout=25, http_client=openai.DefaultHttpxClient(
                           verify=ctx, trust_env=False, event_hooks={'response': [observe]})) as client:
        stores = client.vector_stores
        renamed = stores.update(store_id, name='Thor 项目资料')
        cleared = stores.update(store_id, name=None)
        retrieved = stores.retrieve(store_id)
        listed = [row for row in stores.list(limit=100).data if row.id == store_id]
        assert len(listed) == 1
        names = [item.name for item in (renamed, cleared, retrieved, listed[0])]
    return dict(sdk='python-' + openai.__version__, store=store_id,
                names=names, requests=requests)


if __name__ == '__main__':
    print(json.dumps(run(json.load(sys.stdin))))

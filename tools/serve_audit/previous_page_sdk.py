"""Read adjacent previous pages from a native vector store with a stock Python SDK."""
import json
import ssl
import sys
import time
from urllib.parse import urlsplit

import openai


def run(config):
    target = urlsplit(config['base'])
    assert target.scheme == 'https' and target.hostname == '127.0.0.1'
    requests = []

    def observe(reply):
        req = reply.request
        requests.append(dict(method=req.method, path=req.url.path, query=req.url.query.decode(),
                             status=reply.status_code, trace=reply.headers.get('x-request-id')))
        time.sleep(.125)

    transport = openai.DefaultHttpxClient(verify=ssl.create_default_context(cafile=config['ca']),
        trust_env=False, event_hooks={'response': [observe]})
    pages = []
    with openai.OpenAI(base_url=config['base'], api_key=config['key'], http_client=transport,
                       max_retries=0, timeout=25) as client:
        stores = client.vector_stores
        store = config['store']
        resources = {
            'stores': stores.list,
            'files': lambda **query: stores.files.list(store, filter='completed', **query),
            'batch_files': lambda **query: stores.file_batches.list_files(
                config['batch'], vector_store_id=store, filter='completed', **query),
        }
        for resource, listing in resources.items():
            for order in ('asc', 'desc'):
                ids = config['stores' if resource == 'stores' else 'files']
                ids = ids if order == 'asc' else ids[::-1]
                cursor = ids[-1]
                for end in (4, 2):
                    page = listing(order=order, before=cursor, limit=2)
                    values = [item.id for item in page.data]
                    assert values == ids[end - 2:end]
                    assert (page.first_id, page.last_id, page.has_more) == (values[0], values[-1], end > 2)
                    pages.append(dict(resource=resource, order=order, before=cursor, ids=values,
                                      first=page.first_id, last=page.last_id, more=page.has_more))
                    cursor = page.first_id
    return dict(sdk='python-' + openai.__version__, pages=pages, requests=requests)


if __name__ == '__main__':
    print(json.dumps(run(json.load(sys.stdin))))

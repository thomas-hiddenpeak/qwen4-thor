"""Use the official Python SDK to write or consume a stored project Chat."""
from contextlib import ExitStack
import json
import ssl
import sys
from urllib.parse import urlsplit

import openai


def run(config):
    target = urlsplit(config['base'])
    assert target.scheme == 'https' and target.hostname == '127.0.0.1'
    requests = []

    def observe(reply):
        req = reply.request
        requests.append(dict(method=req.method, path=req.url.path, query=req.url.query.decode('ascii'),
                             status=reply.status_code, trace=reply.headers.get('x-request-id')))

    def summary(chat):
        return dict(id=chat.id, answer=chat.choices[0].message.content, metadata=chat.metadata)

    def denied(callback):
        try:
            callback()
        except openai.NotFoundError as exc:
            return dict(status=exc.status_code, code=exc.code)
        raise ValueError('stored Chat lookup unexpectedly succeeded')

    with ExitStack() as stack:
        def resource(key):
            transport = openai.DefaultHttpxClient(verify=ssl.create_default_context(cafile=config['ca']),
                trust_env=False, event_hooks={'response': [observe]})
            client = stack.enter_context(openai.OpenAI(api_key=key, base_url=config['base'],
                http_client=transport, max_retries=0, timeout=55))
            return client.chat.completions

        owner = resource(config['key'])
        if config['action'] == 'write':
            result = dict(created=summary(owner.create(**config['request'])))
        else:
            assert config['action'] == 'read'
            peer = resource(config['peer'])
            identity = config['id']
            before = summary(owner.retrieve(identity))
            forbidden = denied(lambda: peer.retrieve(identity))
            updated = summary(owner.update(identity, metadata=config['metadata']))
            listing = owner.list(metadata=config['metadata'], limit=1)
            pages = []
            query = dict(order='asc', limit=1)
            for _ in range(2):
                page = owner.messages.list(identity, **query)
                assert len(page.data) == 1
                row = page.data[0]
                pages.append(dict(id=row.id, role=row.role, content=row.content, has_more=page.has_more))
                query['after'] = row.id
            saved = summary(owner.retrieve(identity))
            deleted = owner.delete(identity)
            gone = denied(lambda: owner.retrieve(identity))
            result = dict(before=before, forbidden=forbidden, updated=updated,
                          listed=[r.id for r in listing.data], list_has_more=listing.has_more,
                          pages=pages, saved=saved, deleted=dict(id=deleted.id, deleted=deleted.deleted),
                          gone=gone)
    return dict(sdk='python-'+openai.__version__, requests=requests, **result)


if __name__ == '__main__':
    print(json.dumps(run(json.load(sys.stdin))))

"""Exercise the container lifecycle with an installed stock OpenAI Python SDK."""
from contextlib import ExitStack
from hashlib import sha256
import json
from pathlib import Path
import ssl
import sys

import openai


def run(config):
    requests = []

    def observe(response):
        url = response.request.url
        requests.append(dict(method=response.request.method, path=url.path,
                             query=url.query.decode('ascii'), status=response.status_code,
                             trace=response.headers.get('x-request-id')))

    def missing(call):
        try:
            call()
        except openai.NotFoundError as error:
            assert error.status_code == 404
        else:
            raise AssertionError('Private or deleted resource was visible')

    document = Path(config['document']).read_bytes()
    digest = sha256(document).hexdigest()
    ca = ssl.create_default_context(cafile=config['certificate'])
    with ExitStack() as stack:
        def client(key):
            transport = openai.DefaultHttpxClient(verify=ca, trust_env=False,
                                                  event_hooks={'response': [observe]})
            return stack.enter_context(openai.OpenAI(base_url=config['base'], api_key=key,
                http_client=transport, max_retries=0, timeout=30))

        api = client(config['key']).containers
        peer = client(config['peer']).containers
        box = api.create(name='q4t-sdk-publication')
        assert box.object == 'container' and box.status == 'running'
        assert box.id in [item.id for item in api.list(limit=100).data]
        assert api.retrieve(box.id).name == box.name
        lookup = dict(container_id=box.id)
        file = api.files.create(box.id, file=('release.json', document, 'application/json'))
        note = api.files.create(box.id, file=('sha256.txt', digest.encode(), 'text/plain'))
        ids = [file.id, note.id]
        first = api.files.list(box.id, limit=1, order='asc')
        assert len(first.data) == 1 and first.has_next_page()
        second = first.get_next_page()
        assert len(second.data) == 1 and not second.has_next_page()
        page_ids = [first.data[0].id, second.data[0].id]
        assert len(set(ids)) == 2 and set(page_ids) == set(ids)
        saved = api.files.retrieve(file.id, **lookup)
        assert (saved.container_id, saved.path, saved.bytes) == (
            box.id, '/mnt/data/release.json', len(document))
        content = api.files.content.retrieve(file.id, **lookup).read()
        assert content == document
        missing(lambda: peer.retrieve(box.id))
        missing(lambda: peer.files.content.retrieve(file.id, **lookup))
        api.files.delete(file.id, **lookup)
        missing(lambda: api.files.retrieve(file.id, **lookup))
        api.delete(box.id)
        missing(lambda: api.retrieve(box.id))
    return dict(language='python', version=openai.__version__, container=box.id,
                files=ids, pages=page_ids, bytes=len(content), sha256=sha256(content).hexdigest(),
                requests=requests)


if __name__ == '__main__':
    print(json.dumps(run(json.load(sys.stdin))))

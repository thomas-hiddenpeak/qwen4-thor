"""Exercise conversation ownership, pagination and deletion with a stock SDK."""
from contextlib import ExitStack
import json
import ssl
import sys

import openai


def run(config):
    rows = []

    def observe(response):
        rows.append(dict(method=response.request.method, path=response.request.url.path,
                         query=response.request.url.query.decode('ascii'),
                         status=response.status_code, trace=response.headers.get('x-request-id')))

    def missing(call):
        try:
            call()
        except openai.NotFoundError as error:
            assert error.status_code == 404
        else:
            raise AssertionError('private or deleted resource was visible')

    ca = ssl.create_default_context(cafile=config['certificate'])
    with ExitStack() as stack:
        def client(key):
            transport = openai.DefaultHttpxClient(verify=ca, trust_env=False,
                                                  event_hooks={'response': [observe]})
            return stack.enter_context(openai.OpenAI(base_url=config['base'], api_key=key,
                http_client=transport, max_retries=0, timeout=60))

        owner = client(config['key'])
        peer = client(config['peer'])
        service = owner.conversations
        created = service.create(metadata={'state': 'draft'})
        ident = created.id
        assert created.object == 'conversation' and created.metadata == {'state': 'draft'}
        metadata = {'sdk': openai.__version__, 'state': 'checked'}
        updated = service.update(ident, metadata=metadata)
        assert updated.id == ident and updated.metadata == metadata
        assert service.retrieve(ident).metadata == metadata
        added = service.items.create(ident, items=[
            {'role': 'user', 'content': fact} for fact in config['facts']])
        ids = [item.id for item in added.data]
        assert len(set(ids)) == len(ids) == 2
        first = service.items.list(ident, limit=1, order='asc')
        assert first.has_next_page() and [item.id for item in first.data] == ids[:1]
        last = first.get_next_page()
        assert not last.has_next_page() and [item.id for item in last.data] == ids[1:]
        saved = service.items.retrieve(ids[1], conversation_id=ident)
        assert saved.id == ids[1] and saved.content[0].text == config['facts'][1]
        missing(lambda: peer.conversations.items.retrieve(ids[1], conversation_id=ident))
        answer = None
        if config['generate']:
            response = owner.responses.create(model=config['model'], conversation=ident,
                input=config['question'], temperature=0, max_output_tokens=128)
            answer = response.output_text.strip()
            assert response.status == 'completed' and answer == config['expected'], answer
        removed = service.items.delete(ids[0], conversation_id=ident)
        assert removed.id == ident and removed.object == 'conversation'
        missing(lambda: service.items.retrieve(ids[0], conversation_id=ident))
        deleted = service.delete(ident)
        assert deleted.id == ident and deleted.object == 'conversation.deleted' and deleted.deleted
        missing(lambda: service.retrieve(ident))
    return dict(language='python', version=openai.__version__, conversation=ident,
                items=ids, answer=answer, requests=rows)


if __name__ == '__main__':
    print(json.dumps(run(json.load(sys.stdin))))

"""Use stock SDK helpers to ingest two original release records and read them back."""
from contextlib import ExitStack
import base64
import json
import re
import ssl
import sys
from urllib.parse import urlsplit

import openai


def run(config):
    target = urlsplit(config['base'])
    assert target.scheme == 'https' and target.hostname == '127.0.0.1'
    documents = {name: base64.b64decode(value, validate=True)
                 for name, value in config['documents'].items()}
    assert len(documents) == 2
    requests = []

    def observed(reply):
        req = reply.request
        requests.append(dict(method=req.method, path=req.url.path, query=req.url.query.decode(),
                             status=reply.status_code, trace=reply.headers.get('x-request-id'),
                             helper=req.headers.get('x-stainless-poll-helper')))
        assert len(requests) <= 75, 'polling exceeded the request budget'

    with ExitStack() as stack:
        def client(key):
            transport = openai.DefaultHttpxClient(verify=ssl.create_default_context(cafile=config['ca']),
                trust_env=False, event_hooks={'response': [observed]})
            return stack.enter_context(openai.OpenAI(base_url=config['base'], api_key=key,
                max_retries=0, timeout=40, http_client=transport))

        owner = client(config['key'])
        stores = owner.vector_stores
        if config['action'] == 'write':
            store = stores.create(name='Project release records')
            batch = stores.file_batches.upload_and_poll(store.id, files=[
                (name, data, 'text/plain') for name, data in documents.items()],
                max_concurrency=2, poll_interval_ms=500)
            assert batch.status == 'completed' and batch.file_counts.completed == batch.file_counts.total == 2
            assert batch.file_counts.failed == batch.file_counts.cancelled == batch.file_counts.in_progress == 0
            return dict(version=openai.__version__, store=store.id, batch=batch.id, counts=batch.file_counts.model_dump(), requests=requests)

        assert config['action'] == 'read'
        store = config['store']
        batch_id = config['batch']
        batch = stores.file_batches.retrieve(batch_id, vector_store_id=store)
        assert batch.status == 'completed' and batch.file_counts.completed == batch.file_counts.total == 2
        page = stores.file_batches.list_files(batch_id, vector_store_id=store,
                                             filter='completed', limit=1, order='asc')
        assert len(page.data) == 1 and page.has_next_page()
        last = page.get_next_page()
        assert len(last.data) == 1 and not last.has_next_page()
        entries = [page.data[0], last.data[0]]
        found = {}
        for entry in entries:
            assert entry.vector_store_id == store and entry.status == 'completed'
            name = owner.files.retrieve(entry.id).filename
            assert name in documents and name not in found
            original = json.loads(documents[name])
            pieces = stores.files.content(entry.id, vector_store_id=store).data
            assert len(pieces) == 1 and json.loads(pieces[0].text) == original
            revision = original['publication_revision']
            updated = stores.files.update(entry.id, vector_store_id=store,
                                        attributes={'revision': revision})
            assert updated.attributes == {'revision': revision}
            found[name] = entry.id
        assert set(found) == set(documents) and len(set(found.values())) == 2
        name = max(documents, key=lambda item: json.loads(documents[item])['publication_revision'])
        revision = json.loads(documents[name])['publication_revision']
        match = {'type': 'eq', 'key': 'revision', 'value': revision}
        query = 'What publication revision was registered for this release?'
        search = stores.search(store, query=query, filters=match, max_num_results=1)
        assert len(search.data) == 1 and search.data[0].file_id == found[name]
        assert search.data[0].filename == name and search.data[0].attributes == {'revision': revision}
        assert any(json.loads(chunk.text) == json.loads(documents[name]) for chunk in search.data[0].content)

        try:
            client(config['peer']).vector_stores.file_batches.retrieve(batch_id, vector_store_id=store)
        except openai.NotFoundError:
            pass
        else:
            raise AssertionError('a different client read the batch')

        answer = None
        tool_files = []
        if config['generate']:
            result = owner.responses.create(model='qwen3.8-flash-next', temperature=0,
                reasoning={'effort': 'none'}, max_output_tokens=256, max_tool_calls=1,
                parallel_tool_calls=False, tools=[dict(type='file_search', vector_store_ids=[store],
                    max_num_results=1, filters=match)], tool_choice={'type': 'file_search'},
                include=['file_search_call.results'],
                instructions='Search the release document once. Answer with the integer value of publication_revision.',
                input=query)
            calls = [item for item in result.output if item.type == 'file_search_call']
            assert result.status == 'completed' and len(calls) == 1 and calls[0].status == 'completed'
            tool_files = [hit.file_id for hit in calls[0].results]
            assert tool_files == [found[name]], tool_files
            answer = result.output_text.strip()
            assert set(re.findall(r'(?<![a-zA-Z0-9_])\d+(?![a-zA-Z0-9_])', answer)) == {str(revision)}, answer
        assert stores.delete(store).deleted
        for file_id in found.values():
            assert owner.files.delete(file_id).deleted
        assert not owner.files.list(purpose='assistants').data
    return dict(version=openai.__version__, store=store, batch=batch_id, files=found, answer=answer,
                selected=found[name], revision=revision, counts=batch.file_counts.model_dump(),
                page_ids=[entry.id for entry in entries], tool_files=tool_files, requests=requests)


if __name__ == '__main__':
    print(json.dumps(run(json.load(sys.stdin))))

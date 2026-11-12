"""Actual official Python SDK/HTTP/storage; CPU math vectors, not model quality."""
import json
from pathlib import Path
import ssl
import sys
import time
from urllib.parse import urlsplit

import httpx2 as httpx
import openai


def main():
    base, key, other_key, output, ca = sys.argv[1:]
    assert urlsplit(base).hostname in ('127.0.0.1', 'localhost')
    requests = []
    status = 'FAIL'
    def capture(reply):
        reply.read()
        row = dict(method=reply.request.method, path=reply.request.url.path,
                   status=reply.status_code, response=reply.json())
        if '/vector_stores' in row['path'] and reply.request.content:
            row['request'] = json.loads(reply.request.content)
        requests.append(row)
        time.sleep(.125)  # Stay within the native observation 10 rps budget.
    transport = httpx.Client(verify=ssl.create_default_context(cafile=ca),
                            event_hooks={'response': [capture]})
    clients = [openai.OpenAI(base_url=base, api_key=value, max_retries=0,
                            timeout=15, http_client=transport)
               for value in (key, other_key)]
    client, other = clients
    def missing(call):
        try:
            call()
        except openai.NotFoundError:
            return
        raise AssertionError('cross-scope or removed resource was readable')
    def wait(get):
        for _ in range(25):
            value = get()
            if value.status == 'completed':return value
            assert value.status == 'in_progress', value
            time.sleep(.15)
        raise AssertionError('Native ingestion did not settle')
    try:
        text = 'Project record: release 80 preserves source-bound document retrieval.\n'
        a = client.files.create(file=('STATUS.md', text.encode()), purpose='user_data')
        b = client.files.create(file=('release.md', text.encode()), purpose='user_data')
        v = client.vector_stores
        store = v.create(name='项目记录', file_ids=[a.id])
        second = v.create(name='第二库')
        assert v.update(store.id, metadata={'stage':'80'}).metadata == {'stage':'80'}
        assert v.retrieve(store.id).name == '项目记录'
        missing(lambda: other.vector_stores.retrieve(store.id))
        v.files.create(store.id, file_id=b.id, attributes={'release':80})
        wait(lambda: v.files.retrieve(a.id, vector_store_id=store.id))
        wait(lambda: v.files.retrieve(b.id, vector_store_id=store.id))
        content = v.files.content(a.id, vector_store_id=store.id)
        assert [item.text for item in content.data] == [text]
        assert v.files.update(b.id, vector_store_id=store.id,
                              attributes={'release':81}).attributes['release'] == 81
        page = v.files.list(store.id, limit=1, order='asc', filter='completed')
        assert len(page.data) == 1 and page.has_next_page()
        assert len(page.get_next_page().data) == 1
        batch = v.file_batches.create(second.id, file_ids=[a.id,b.id])
        assert batch.object == 'vector_store.files_batch'
        wait(lambda: v.file_batches.retrieve(batch.id, vector_store_id=second.id))
        assert len(v.file_batches.list_files(batch.id, vector_store_id=second.id,
                                              filter='completed').data) == 2
        assert v.file_batches.cancel(batch.id, vector_store_id=second.id).status == 'completed'
        page = v.list(limit=1)
        assert len(page.data) == 1 and page.has_next_page()
        assert len(page.get_next_page().data) == 1
        try:v.list(limit=0)
        except openai.BadRequestError:pass
        else:raise AssertionError('invalid page accepted')
        missing(lambda: other.vector_stores.files.content(a.id, vector_store_id=store.id))
        assert v.files.delete(a.id, vector_store_id=store.id).deleted
        assert client.files.retrieve(a.id).id == a.id
        missing(lambda: v.files.content(a.id, vector_store_id=store.id))
        assert v.delete(store.id).deleted and v.delete(second.id).deleted
        assert client.files.delete(a.id).deleted and client.files.delete(b.id).deleted
        assert len(requests) <= 100
        status = 'PASS'
        print(json.dumps(dict(status=status, requests=len(requests))))
    finally:
        transport.close()
        Path(output).write_text(json.dumps(dict(status=status, sdk_version=openai.__version__,
            requests=requests, actual_network=True, model_starts=0, max_retries=0,
            vectors='explicit_CPU_math_fixture'), ensure_ascii=False, indent=2)+'\n')


if __name__ == '__main__':main()

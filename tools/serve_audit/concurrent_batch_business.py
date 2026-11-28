"""Submit project-record Batches and cancel one through independent SDK clients."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import ssl
import sys
import threading
import time

import openai

sys.path.insert(0, str(Path(__file__).resolve().parent))
from batch_cancel_receipt import batch_input, check_loopback


def run(config):
    check_loopback(config)
    source = config['document']
    revision = json.loads(source)['publication_revision']
    client_count = config.get('callers', 4)
    if type(client_count) is not int or client_count not in (2, 4):
        raise ValueError('expected two or four independent cancellation clients')
    requests = []
    capture_lock = threading.Lock()
    tls = ssl.create_default_context(cafile=config.get('certificate'))

    def observe(reply):
        req = reply.request
        item = dict(method=req.method, path=req.url.path, query=req.url.query.decode('ascii'),
                    status=reply.status_code, trace=reply.headers.get('x-request-id'),
                    start_ns=req.extensions['batch_start_ns'], headers_ns=time.monotonic_ns())
        if 'cancel_client' in req.extensions:
            item.update(client=req.extensions['cancel_client'], attempt=req.extensions['cancel_attempt'])
        if reply.status_code == 429:
            reply.read()
            item.update(error=reply.json()['error']['code'], retry_after=reply.headers.get('retry-after'))
        with capture_lock:
            requests.append(item)

    try:
        with ExitStack() as stack:
            def client(key, number=None):
                attempts = 0
                def starting(request):
                    nonlocal attempts
                    if number is not None:
                        attempts += 1
                        if attempts == 1:
                            barrier.wait()
                        request.extensions.update(cancel_client=number, cancel_attempt=attempts)
                    request.extensions['batch_start_ns'] = time.monotonic_ns()
                transport = openai.DefaultHttpxClient(verify=tls, trust_env=False,
                                                       event_hooks={'request': [starting], 'response': [observe]})
                return stack.enter_context(openai.OpenAI(base_url=config['base'], api_key=key,
                    http_client=transport, max_retries=0, timeout=45))

            owner = client(config['key'])
            peer = client(config['peer'])
            barrier = threading.Barrier(client_count, timeout=8)
            callers = [client(config['key'], i) for i in range(client_count)]

            def submit(label, count):
                raw = batch_input(source,config['model'],label,count)
                upload = owner.files.create(purpose='batch', file=(label+'.jsonl',raw,'application/jsonl'))
                assert upload.bytes == len(raw)
                metadata = dict(project='q4t', scenario=label, revision=str(revision))
                job = owner.batches.create(input_file_id=upload.id, endpoint='/v1/chat/completions',
                                           completion_window='24h', metadata=metadata)
                assert job.input_file_id == upload.id and job.metadata == metadata
                return dict(id=job.id, input=upload.id, source_sha256=hashlib.sha256(raw).hexdigest(),
                            rows=count, metadata=metadata)

            completed = submit('publication', 1)
            pending = submit('cancel', 12)

            def cancel(index):
                for attempt in range(1, 5):
                    try:
                        result = callers[index].batches.cancel(pending['id'])
                    except openai.RateLimitError as exc:
                        if (attempt == 4 or exc.response.json()['error']['code'] != 'gateway_rate_limit'
                                or exc.response.headers.get('retry-after') != '1'):
                            raise
                        time.sleep(1 + .15 * index)
                    else:
                        return dict(client=index, id=result.id, status=result.status,
                                    cancelling_at=result.cancelling_at)

            with ThreadPoolExecutor(max_workers=client_count) as pool:
                receipts = list(pool.map(cancel, range(client_count)))
            assert all(r['id'] == pending['id'] and r['status'] in ('cancelling','cancelled')
                       and type(r['cancelling_at']) is int and r['cancelling_at'] > 0 for r in receipts)
            assert len({r['cancelling_at'] for r in receipts}) == 1
            try:
                peer.batches.cancel(pending['id'])
            except openai.NotFoundError:
                pass
            else:
                raise ValueError('foreign client could cancel the Batch')
            return dict(sdk='python-'+openai.__version__, completed=completed, pending=pending,
                        cancel=receipts, revision=revision, requests=requests)
    finally:
        if config.get('capture'):
            with Path(config['capture']).open('x') as stream:
                json.dump(requests, stream)


if __name__ == '__main__':
    print(json.dumps(run(json.load(sys.stdin))))

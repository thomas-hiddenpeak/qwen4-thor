"""Run concurrent Uploads.complete via two independent official SDK clients."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import base64
import hashlib
import json
import ssl
import sys
import threading
import time
from urllib.parse import urlsplit

import openai


def run(config):
    url = urlsplit(config['base'])
    assert url.scheme == 'https' and url.hostname == '127.0.0.1'
    phase = config.get('phase', 'single')
    assert phase in ('single', 'primary', 'light')
    source = base64.b64decode(config['source'], validate=True)
    expected = str(json.loads(source)['version_revision'])
    digest = hashlib.md5(source, usedforsecurity=False).hexdigest()
    wire = []
    cert = ssl.create_default_context(cafile=config['certificate'])

    def client(stack, key, barrier=None):
        def starting(request):
            if barrier is not None:
                barrier.wait(timeout=10)
            request.extensions['handoff_start_ns'] = time.monotonic_ns()

        def received(response):
            req = response.request
            wire.append(dict(method=req.method, path=req.url.path, status=response.status_code,
                             trace=response.headers.get('x-request-id'), start_ns=req.extensions['handoff_start_ns'],
                             headers_ns=time.monotonic_ns()))

        transport = openai.DefaultHttpxClient(verify=cert, trust_env=False,
            event_hooks={'request': [starting], 'response': [received]})
        return stack.enter_context(openai.OpenAI(http_client=transport, base_url=config['base'],
                                    api_key=key, max_retries=0, timeout=65))

    with ExitStack() as stack:
        owner, peer = client(stack, config['key']), client(stack, config['peer'])
        restored = None
        if phase == 'light':
            retained = config['previous']['pairs'][0]
            receipt = owner.uploads.complete(retained['upload'], part_ids=[retained['part']], md5=digest)
            assert receipt.status == 'completed' and receipt.file.id == retained['file']
            assert owner.files.content(receipt.file.id).read() == source
            assert owner.files.delete(receipt.file.id).deleted
            restored = receipt.file.id
        pairs = []
        for same in (True, False):
            upload = owner.uploads.create(filename='actual-release.txt', mime_type='text/plain',
                                         purpose='user_data', bytes=len(source))
            part = owner.uploads.parts.create(upload.id, data=('release.txt', source))
            barrier = threading.Barrier(2)
            competitors = [client(stack, config['key'], barrier) for _ in range(2)]

            def finish(number):
                options = {'md5': digest} if same or number == 0 else {}
                try:
                    record = competitors[number].uploads.complete(upload.id, part_ids=[part.id], **options)
                    assert record.id == upload.id and record.status == 'completed'
                    return {'status': 200, 'file': record.file.id}
                except openai.ConflictError as error:
                    assert error.code == 'upload_already_completed'
                    return {'status': error.status_code, 'code': error.code}

            previous = len(wire)
            with ThreadPoolExecutor(max_workers=2) as pool:
                replies = list(pool.map(finish, (0, 1)))
            attempts = wire[previous:]
            require_codes = [200, 200] if same else [200, 409]
            assert sorted(r['status'] for r in replies) == require_codes
            assert len(attempts) == 2 and all(r['method'] == 'POST' and
                r['path'] == '/v1/uploads/'+upload.id+'/complete' for r in attempts)
            assert max(r['start_ns'] for r in attempts) < min(r['headers_ns'] for r in attempts)
            files = {r['file'] for r in replies if r['status'] == 200}
            assert len(files) == 1
            pairs.append(dict(same_parameters=same, upload=upload.id, part=part.id, file=files.pop(), replies=replies))
        ids = {r['file'] for r in pairs}
        listing = owner.files.list(purpose='user_data', limit=50)
        assert len(ids) == len(listing.data) == 2 and {f.id for f in listing.data} == ids
        assert not listing.has_more
        for pair in pairs:
            assert owner.files.content(pair['file']).read() == source
        try:
            peer.files.retrieve(pairs[0]['file'])
        except openai.NotFoundError:
            pass
        else:
            raise AssertionError('peer accessed owner file')
        response = owner.responses.create(model=config['model'], temperature=0, max_output_tokens=160,
            input=[dict(role='user', content=[
                dict(type='input_file', file_id=pairs[0]['file']),
                dict(type='input_text', text='What integer does version_revision contain in this record? Reply only with the number.')])])
        assert response.status == 'completed' and response.output_text.strip() == expected
        for pair in (pairs[1:] if phase == 'primary' else pairs):
            assert owner.files.delete(pair['file']).deleted
        remaining = [file.id for file in owner.files.list(purpose='user_data').data]
        assert remaining == ([pairs[0]['file']] if phase == 'primary' else [])
        chat = owner.chat.completions.create(model=config['model'], temperature=0, max_completion_tokens=64,
            extra_body={'enable_thinking':False}, messages=[dict(role='user', content='Compute 17 plus 25. Reply only with the number.')])
        assert chat.choices[0].message.content.strip() == '42'
    count = {'single': 17, 'primary': 16, 'light': 20}[phase]
    assert len(wire) == count and all(r['trace'] for r in wire) and len({r['trace'] for r in wire}) == count
    return dict(version=openai.__version__, phase=phase, restored=restored, pairs=pairs, requests=wire, response=response.output_text.strip(),
                independent_chat=chat.choices[0].message.content.strip(), source_sha256=hashlib.sha256(source).hexdigest())


if __name__ == '__main__':
    print(json.dumps(run(json.load(sys.stdin))))

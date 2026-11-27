"""Exercise an isolated owner's uploads before and after a managed package handoff."""
import base64
import hashlib
import json
from pathlib import Path
import subprocess
from urllib.parse import parse_qsl

HERE = Path(__file__).resolve().parent
OLD, MIDDLE, RECENT, CURRENT = ('python-2.8.1', 'python-2.33.0', 'python-3.42.1', 'python-3.55.1')
NODE_OLD, NODE_CURRENT = 'node-6.34.0', 'node-7.47.0'


class UploadHandoff:
    def __init__(self, *, sdk, base, keys, source, trace):
        self.sdk = sdk
        self.base = base
        self.keys = keys
        self.source = source
        self.trace = trace
        self.identities = set()
        self.count = 0

    def call(self, sdk, action, method, path, *, status=200, identity=None, body=None, owner='alpha'):
        assert self.count < 25
        self.count += 1
        language, version = sdk.split('-', 1)
        executable = self.sdk[sdk]
        command = ([executable, '-I', '-B', HERE/'upload_business.py'] if language == 'python'
                   else ['node', HERE/'upload_business.mjs', executable])
        request = dict(base=self.base, key=self.keys[owner], action=action, id=identity,
                       body=body or {}, version=version)
        result = subprocess.run(command, input=json.dumps(request), text=True, capture_output=True,
                                timeout=75)
        if result.returncode:
            details = result.stderr.replace(request['key'], '<redacted>')
            raise RuntimeError(f'{sdk} {action}: {details[-3000:]}')
        reply = json.loads(result.stdout)
        wire = reply['wire']
        with self.trace.open('a') as file:
            file.write(json.dumps(dict(step=self.count, sdk=sdk, action=action, owner=owner,
                                       wire=wire, data=reply['data']))+'\n')
        assert reply['sdk'] == sdk and (wire['method'], wire['path'], wire['status']) == (
            method, '/v1'+path, status), (action, wire, reply['data'])
        expected_query = [('limit', '50'), ('purpose', 'user_data')] if action == 'list' else []
        actual_query = parse_qsl(wire['query'], strict_parsing=True, keep_blank_values=True)
        if sorted(actual_query) != expected_query:
            raise ValueError(f'{sdk} used unexpected query parameters for {action}')
        trace = wire['trace']
        assert isinstance(trace, str) and trace and trace not in self.identities, wire
        self.identities.add(trace)
        return reply['data']

    def create(self, sdk, filename):
        data = self.call(sdk, 'upload', 'POST', '/uploads', body=dict(
            filename=filename, bytes=len(self.source), mime_type='text/plain', purpose='user_data'))
        assert data['status'] == 'pending' and data['filename'] == filename
        return data['id']

    def part(self, sdk, upload, content, status=200):
        data = self.call(sdk, 'part', 'POST', f'/uploads/{upload}/parts', identity=upload,
                         body={'data': base64.b64encode(content).decode()}, status=status)
        if status == 200:
            assert data['object'] == 'upload.part' and data['upload_id'] == upload
            return data['id']
        assert data['error'] == 'upload_not_pending', data

    def complete(self, sdk, upload, parts, checksum, status=200):
        data = self.call(sdk, 'complete', 'POST', f'/uploads/{upload}/complete', identity=upload,
                         body={'part_ids': parts, 'md5': checksum}, status=status)
        if status == 200:
            assert data['id'] == upload and data['status'] == 'completed'
            return data['file']
        assert data['error'] == 'invalid_request', data

    def prepare(self):
        head, tail = self.split()
        check = hashlib.md5(self.source, usedforsecurity=False).hexdigest()
        first = self.create(OLD, 'primary-release.txt')
        last_part = self.part(NODE_OLD, first, tail)
        first_part = self.part(MIDDLE, first, head)
        parts = [first_part, last_part]
        published = self.complete(RECENT, first, parts, check)
        pending = self.create(CURRENT, 'fallback-release.txt')
        pending_tail = self.part(NODE_CURRENT, pending, tail)
        cancel = self.create(MIDDLE, 'cancelled-release.txt')
        direct = self.call(OLD, 'file', 'POST', '/files', body={
            'data': base64.b64encode(self.source).decode(), 'filename': 'direct-release.txt'})
        assert self.count == 8 and len({first, pending, cancel}) == 3
        return dict(first=first, parts=parts, published=published, pending=pending,
                    tail=pending_tail, cancel=cancel, direct=direct, md5=check)

    def split(self):
        middle = len(self.source)//2
        assert middle > 0
        return self.source[:middle], self.source[middle:]

    def download(self, sdk, file):
        data = self.call(sdk, 'download', 'GET', f'/files/{file}/content', identity=file)
        assert data == dict(bytes=len(self.source), sha256=hashlib.sha256(self.source).hexdigest())

    def restore(self, prior):
        file = prior['published']
        first = file['id']
        repeated = self.complete(OLD, prior['first'], prior['parts'], prior['md5'])
        assert repeated == file
        metadata = self.call(CURRENT, 'retrieve', 'GET', '/files/'+first, identity=first)
        assert metadata == file
        self.download(MIDDLE, first)
        pending = prior['pending']
        head = self.part(NODE_OLD, pending, self.split()[0])
        parts = [head, prior['tail']]
        wrong = ('0' if prior['md5'][0] != '0' else '1') + prior['md5'][1:]
        self.complete(RECENT, pending, parts, wrong, status=400)
        restored = self.complete(CURRENT, pending, parts, prior['md5'])
        second = restored['id']
        assert restored['filename'] == 'fallback-release.txt' and restored['bytes'] == len(self.source)
        self.download(OLD, second)
        self.call(MIDDLE, 'retrieve', 'GET', '/files/'+second, identity=second, owner='beta', status=404)
        cancel = prior['cancel']
        cancelled = self.call(NODE_CURRENT, 'cancel', 'POST', f'/uploads/{cancel}/cancel', identity=cancel)
        assert cancelled['id'] == cancel and cancelled['status'] == 'cancelled'
        self.part(NODE_OLD, cancel, b'late', status=409)
        ids = {first, second, prior['direct']['id']}
        listing = self.call(CURRENT, 'list', 'GET', '/files')
        assert len(ids) == len(listing['ids']) == 3 and set(listing['ids']) == ids and not listing['has_more']
        question = 'What integer does version_revision contain in the attached release record? Reply only with the number.'
        result = self.call(CURRENT, 'answer', 'POST', '/responses', body=dict(
            model='qwen3.8-flash-next', temperature=0, max_output_tokens=160,
            input=[dict(role='user', content=[dict(type='input_file', file_id=second),
                                              dict(type='input_text', text=question)])]))
        assert result['status'] == 'completed' and result['answer'].strip() == str(
            json.loads(self.source)['version_revision']), result
        for sdk, identity in ((RECENT, first), (OLD, second), (MIDDLE, prior['direct']['id'])):
            deleted = self.call(sdk, 'delete', 'DELETE', '/files/'+identity, identity=identity)
            assert deleted['id'] == identity and deleted['deleted']
        self.call(CURRENT, 'retrieve', 'GET', '/files/'+second, identity=second, status=404)
        assert self.call(MIDDLE, 'list', 'GET', '/files') == dict(ids=[], has_more=False)
        assert self.count == 25
        return dict(source_sha256=hashlib.sha256(self.source).hexdigest(), repeated=first,
                    resumed=second, response=result['id'], answer=result['answer'].strip(),
                    sdk_requests=self.count)

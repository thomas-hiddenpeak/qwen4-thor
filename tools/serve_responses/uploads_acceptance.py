"""Uploads business: one model, two generations, 32 file operations, 600s.

Actual SDK, direct core/native TLS, document handoff and a real tool closure.
No performance acceptance or unrelated model matrix. Guardian evidence reused.
"""
import argparse
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_responses import files_acceptance as files
from tools.serve_service.package import read, require, save, sha

LIMITS = dict(starts=1, chats=2, files=32, controls=2000, seconds=600)
SOURCES = tuple('tools/serve_responses/'+n for n in (
    'uploads_acceptance.py', 'run_uploads_sdk.mjs', 'test_uploads_acceptance.py'))


def requests():
    return {k: v for k, v in files.requests().items() if k in ('tool', 'follow')}


def create_body(raw):
    return dict(bytes=len(raw), purpose='user_data', filename='MODEL.md', mime_type='text/plain')


def part_body(raw):
    return dict(data_base64=base64.b64encode(raw).decode())


def completion(parts, raw):
    return dict(part_ids=parts, md5=hashlib.md5(raw).hexdigest())


def prepare(args):
    files.background.prepare(args, rows=requests(), limits=LIMITS, scope=__doc__)
    out = args.output.resolve();path = out/'plan.json';plan = read(path)
    files.certificates(out/'tls')
    native = args.ingress.resolve(strict=True)
    plan.update(ingress=dict(path=str(native), sha256=sha(native)), edge_port=args.port+2,
        tls_inputs={str(p.relative_to(out)): sha(p) for p in (out/'tls').iterdir() if p.is_file()},
        mutations=['one isolated actual model and native TLS entry; no existing service changes',
            'private Uploads/parts/files only; completion checksum, cancellation and owner rejection'],
        derived_requests=['upload real docs/MODEL.md in two parts, complete in explicit original order',
            'read completed file through actual SDK and use original bytes for actual tool execution',
            'repeat one binary Part, reject wrong MD5, complete once and reconcile exact original file',
            'cancel pending Upload, reject late Part, no file operations consume generation RPM/TPM',
            'native parent admission is an identity-bound test adapter; Guardian recovery reused'])
    plan['sources'].update({name: sha(ROOT/name) for name in SOURCES})
    save(path, plan)


class Stage(files.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_UPLOADS_TARGETED_BUSINESS'

    def uploads(self, label, action, body=None, *, identity=None, direct=False,
                other=False, expected_status=200):
        self.budget('files');self.budget('controls')
        command = dict(action=action, request=body, id=identity,
            sdk_directory=self.plan['sdk']['sdk_directory'],
            api_key=files.OTHER_KEY if other else files.KEY,
            base_url=f'http://127.0.0.1:{self.plan["port"]}/v1' if direct else self.edge.url)
        if direct:
            command['headers'] = dict(files.encrypted.HEADERS,
                **({'X-Q4T-Client-Id': files.OTHER} if other else {}))
        env = {k: v for k, v in os.environ.items() if k not in ('NODE_OPTIONS', 'NODE_PATH', 'LD_PRELOAD')}
        env['NODE_EXTRA_CA_CERTS'] = str(self.out/'tls/ca.pem')
        run = subprocess.run(['node', str(ROOT/'tools/serve_responses/run_uploads_sdk.mjs')],
            input=json.dumps(command), capture_output=True, text=True, env=env, timeout=20)
        (self.out/(label+'.stderr')).write_text(run.stderr)
        require(not run.stderr and run.stdout, 'Uploads SDK bridge failed')
        value = json.loads(run.stdout);save(self.out/(label+'.sdk.json'), value)
        files.check_exchange(value, expected_status)
        require(run.returncode == (0 if value['ok'] else 1), 'Uploads exit differs')
        if value['ok']:
            name = 'upload_part_resource' if action == 'part' else 'upload_resource'
            require(not files.errors(value['result'], name), 'actual Uploads response schema')
        require(self.edge.error is None and self.edge.process.poll() is None, 'native entry changed')
        self.same_model()
        return value.get('result')

    def business(self):
        caps = read(self.out/'capabilities-1.json')['uploads']
        require(caps['profile'] == 'q4t-uploads-v1' and caps['complete'] is True and
                caps['cancel'] is True and not caps['core_readiness_dependency'], 'Uploads discovery')
        document = (ROOT/files.foreground.DOCUMENT).read_bytes()
        upload = self.uploads('create-document', 'create', create_body(document), direct=True)
        middle = len(document)//2
        tail = self.uploads('part-tail', 'part', part_body(document[middle:]), identity=upload['id'])
        head = self.uploads('part-head', 'part', part_body(document[:middle]), identity=upload['id'])
        completed = self.uploads('complete-document', 'complete',
            completion([head['id'], tail['id']], document), identity=upload['id'])
        require(completed['status'] == 'completed', 'document not completed')
        file = completed['file']
        require(self.files('document-metadata-core', 'retrieve', file_id=file['id'], direct=True) == file,
                'completed File metadata differs')
        content = self.files('document-content', 'content', file_id=file['id'])
        files.check_download(content, document)
        tool = self.sdk('tool', 'create', self.plan['requests']['tool'])
        calls = [item for item in tool['output'] if item['type'] == 'function_call']
        require(tool['status'] == 'completed' and len(calls) == 1 and
            calls[0]['name'] == 'read_project_record' and calls[0]['status'] == 'completed' and
            json.loads(calls[0]['arguments']) == dict(path=files.foreground.DOCUMENT, start_line=1, line_count=3),
            'actual tool call differs')
        result = files.tool_result(base64.b64decode(content['data_base64']),
                                   self.plan['sources'][files.foreground.DOCUMENT])
        save(self.out/'tool-execution.json', dict(call=calls[0], upload_id=upload['id'],
                                                file_id=file['id'], result=result))
        follow = copy.deepcopy(self.plan['requests']['follow'])
        follow.update(previous_response_id=tool['id'], input=[dict(type='function_call_output',
            call_id=calls[0]['call_id'], output=json.dumps(result, ensure_ascii=False))])
        final = self.sdk('follow', 'create', follow)
        answer = json.loads(files.foreground.text(final));title = document.decode().splitlines()[0]
        require(final['status'] == 'completed' and answer['title'] in (title, title.lstrip('# ')) and
                answer['citation'] == result['source']['citation'], 'actual document tool closure')
        raw = bytes(range(256));both = raw+raw
        repeated = self.uploads('create-repeat', 'create', dict(create_body(both),
            filename='repeat.bin', purpose='evals', mime_type='application/octet-stream'))
        part = self.uploads('part-repeat', 'part', part_body(raw), identity=repeated['id'], direct=True)
        body = completion([part['id'], part['id']], both)
        self.uploads('wrong-checksum', 'complete', dict(body, md5='0'*32),
                     identity=repeated['id'], expected_status=400)
        copied = self.uploads('complete-repeat', 'complete', body, identity=repeated['id'])
        require(self.uploads('reconcile-repeat', 'complete', body, identity=repeated['id']) == copied,
                'completion reconciliation changed File')
        files.check_download(self.files('repeat-content', 'content', file_id=copied['file']['id']), both)
        pending = self.uploads('create-cancel', 'create', create_body(b'abc'))
        self.uploads('part-cancel', 'part', part_body(b'abc'), identity=pending['id'])
        self.uploads('foreign-cancel', 'cancel', identity=pending['id'], other=True, expected_status=404)
        cancelled = self.uploads('cancel', 'cancel', identity=pending['id'])
        require(cancelled['status'] == 'cancelled', 'Upload not cancelled')
        self.uploads('late-part', 'part', part_body(b'abc'), identity=pending['id'], expected_status=409)
        for name, value in [('document', file), ('repeat', copied['file'])]:
            self.files('delete-'+name, 'delete', file_id=value['id'])
        require(not self.files('empty-list', 'list')['data'], 'unwanted File publication')
        self.accounting('final-accounting', [tool, final]);self.same_model()
        save(self.out/'business-checks.json', dict(native_tls=True, actual_sdk=True,
            exact_file_handoff=True, real_document_tool_closure=True, repeated_part_bytes=True,
            wrong_md5_preserves_pending=True, completion_reconciles_original=True,
            owner_isolation=True, cancelled_late_part_rejected=True, exact_generation_usage=True,
            file_input_resolution=False, native_parent_admission='identity-bound test adapter',
            performance_claim=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    command = modes.add_parser('prepare')
    for name in ('binary', 'ingress', 'output'):command.add_argument('--'+name, type=Path, required=True)
    command.add_argument('--port', type=int, default=19011)
    command = modes.add_parser('run');command.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'prepare':prepare(args)
    else:
        def timeout(_signal, _frame):raise TimeoutError('Uploads business deadline')
        prior = signal.signal(signal.SIGALRM, timeout)
        try:
            signal.setitimer(signal.ITIMER_REAL, LIMITS['seconds']-40)
            with files.base.lifecycle.child_subreaper():Stage(read(args.plan)).run()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0);signal.signal(signal.SIGALRM, prior)


if __name__ == '__main__':main()

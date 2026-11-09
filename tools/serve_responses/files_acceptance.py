"""A09 Files: one real model, three generations, native TLS and own IO fault.

The native entry's parent admission peer is a bounded test adapter tied to the
real model identity. Guardian recovery is reused, not claimed retested here.
"""
import argparse
import base64
import copy
import hashlib
import http.client
import json
import os
from pathlib import Path
import signal
import ssl
import subprocess
import sys
import threading
import time
import uuid

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_responses import encryption_acceptance as encrypted
from tools.serve_responses import background_acceptance as background
from tools.serve_responses.reconciliation_acceptance import own_object_worker
from tools.serve_responses.test_contract import errors
from tools.serve_recovery import journal, native_ingress
from tools.serve_recovery.test_gateway import config as gateway_fixture
from tools.serve_recovery.test_native_access import certificates
from tools.serve_gateway import gateway
from tools.serve_service.package import read, require, save, sha

foreground = encrypted.foreground
base = foreground.base
LIMITS = dict(starts=1, chats=3, files=32, controls=2000, seconds=600)
FILE_LIMIT = 32*1024**2
OTHER = 'files-other'
KEY, OTHER_KEY = 'A'*40, 'B'*40


def requests():
    original = foreground.requests()
    return dict(tool=original['tool'], follow=original['follow'],
        healthy=dict(original['plain'], stream=True,
                     stream_options=dict(include_obfuscation=False)))


def check_download(value, raw):
    require(value['bytes'] == len(raw) and value['sha256'] == hashlib.sha256(raw).hexdigest()
            and base64.b64decode(value['data_base64'], validate=True) == raw,
            'actual Files bytes differ')


def tool_result(raw, expected_sha256):
    require(hashlib.sha256(raw).hexdigest() == expected_sha256, 'downloaded document identity')
    lines = raw.decode('utf-8').splitlines()[:3]
    require(len(lines) == 3, 'actual document lines missing')
    return dict(ok=True, source=dict(path=foreground.DOCUMENT, start_line=1,
        line_count=3, sha256=expected_sha256, citation='[docs/MODEL.md:L1-L3]',
        text='\n'.join(str(i+1)+': '+line for i, line in enumerate(lines))))


def prepare(args):
    background.prepare(args, rows=requests(), limits=LIMITS, scope=__doc__)
    out = args.output.resolve();path = out/'plan.json';plan = read(path)
    certificates(out/'tls')
    native = args.ingress.resolve(strict=True)
    plan['ingress'] = dict(path=str(native), sha256=sha(native))
    plan['edge_port'] = args.port+2
    plan['tls_inputs'] = {str(p.relative_to(out)): sha(p) for p in (out/'tls').iterdir() if p.is_file()}
    plan['mutations'] = ['one own model and native TLS entry with isolated object/client state',
        'SIGSTOP identity-verified own object worker; never signal/restart model',
        'bounded Files read observes storage failure; stateless SSE succeeds on same model']
    plan['derived_requests'] = ['actual file IDs only; multipart SDK upload of real docs/MODEL.md and binary bytes',
        'TLS and direct core metadata/content, owner isolation and delete through actual SDK',
        'real function call output comes from downloaded document; final JSON title/citation',
        'stored file recovers after bounded IO worker recovery; no mutation replay',
        'Files do not consume generation RPM/TPM; exact three-generation usage',
        'native parent admission is a test adapter, not a Guardian recovery acceptance']
    save(path, plan)


class Edge:
    def __init__(self, stage):
        self.stage = stage;self.process = self.peer = self.thread = None
        self.stop = threading.Event();self.error = None
        self.url = f'https://127.0.0.1:{stage.plan["edge_port"]}/files-test/v1'
        source = gateway_fixture(stage.out)['policy']
        source.update(version=5, listen_port=stage.plan['edge_port'],
            upstream_port=stage.plan['port'], upstream_operations_port=stage.plan['operations_port'],
            body_bytes=1048576, file_body_bytes=FILE_LIMIT, per_client_rps=100, burst=100,
            clients=[dict(id=encrypted.CLIENT, api_key=KEY), dict(id=OTHER, api_key=OTHER_KEY)],
            http=dict(path_prefix='/files-test'))
        address = 'q4t-files-business-'+str(uuid.uuid4())
        settings = dict(version=2, parent_pid=os.getpid(),
            parent_start_ticks=journal.process_start(os.getpid()), address=address,
            policy=gateway.policy(source), tls=dict(certificate=str(stage.out/'tls/server.pem'),
            key=str(stage.out/'tls/server.key'), ca='', require_client_certificate=False),
            tls_lease='q4t-files-business-tls-'+str(uuid.uuid4()))
        save(stage.out/'ingress.json', settings)
        def decide(lane):
            stage.same_model()
            return dict(port=stage.plan['operations_port' if lane == 'control' else 'port'],
                        token=encrypted.HEADERS['X-Q4T-Gateway-Token'])
        self.peer = native_ingress.AdmissionSocket(address, [encrypted.CLIENT, OTHER],
            decide, lambda: self.process.pid if self.process is not None else None)
        try:
            with (stage.out/'ingress.log').open('xb') as log:
                self.process = subprocess.Popen([stage.plan['ingress']['path'], '--config',
                    str(stage.out/'ingress.json')], stdin=subprocess.DEVNULL,
                    stdout=log, stderr=log)
            stage.history[self.process.pid] = base.lifecycle.guard.processes()[self.process.pid]
            def serve():
                try:
                    while not self.stop.is_set():self.peer.step(.02)
                except BaseException as error:self.error = error
            self.thread = threading.Thread(target=serve);self.thread.start()
            context = ssl.create_default_context(cafile=str(stage.out/'tls/ca.pem'))
            end = time.monotonic()+5
            while time.monotonic() < end:
                conn = http.client.HTTPSConnection('127.0.0.1', stage.plan['edge_port'],
                                                   context=context, timeout=1)
                try:
                    stage.budget('controls')
                    conn.request('GET', '/files-test/v1/models', headers={'Authorization': 'Bearer '+KEY})
                    response = conn.getresponse();response.read()
                    if response.status == 200:break
                except (OSError, http.client.HTTPException):pass
                finally:conn.close()
                require(self.process.poll() is None and self.error is None, 'native edge failed')
                time.sleep(.02)
            else:raise TimeoutError('native edge readiness')
        except BaseException:
            self.close();raise

    def close(self):
        if self.process is not None and self.process.poll() is None:
            identity = self.stage.history[self.process.pid]
            base.lifecycle.guard.signal_identity(self.process.pid, identity, signal.SIGTERM)
            try:self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                base.lifecycle.guard.signal_identity(self.process.pid, identity, signal.SIGKILL)
                self.process.wait(timeout=3)
        self.stop.set()
        if self.thread:self.thread.join(timeout=3)
        if self.peer:self.peer.close();self.peer = None


class Stage(encrypted.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_FILES_TARGETED_BUSINESS'

    def __init__(self, plan):
        super().__init__(plan)
        self.counts['files'] = 0;self.edge = None
        require(sha(plan['ingress']['path']) == plan['ingress']['sha256'], 'native entry changed')
        for name, digest in plan['tls_inputs'].items():
            require(sha(self.out/name) == digest, 'TLS input changed')

    def client_policy(self):
        value = super().client_policy();value['version'] = 4
        value['clients'][0]['max_file_body_bytes'] = FILE_LIMIT
        value['clients'].append(dict(value['clients'][0], id=OTHER))
        return value

    def start(self):
        background.probe_ports((self.plan['edge_port'],))
        super().start()
        caps = read(self.out/'capabilities-1.json')
        require(caps['files']['binary_streaming'] and not caps['files']['core_readiness_dependency'],
                'Files discovery not implemented')
        self.edge = Edge(self)

    def files(self, label, action, request=None, *, file_id=None,
              direct=False, other=False, expected_status=200):
        self.budget('files');self.budget('controls')
        command = dict(action=action, request=request, id=file_id,
            sdk_directory=self.plan['sdk']['sdk_directory'], api_key=OTHER_KEY if other else KEY,
            base_url=f'http://127.0.0.1:{self.plan["port"]}/v1' if direct else self.edge.url)
        if direct:
            command['headers'] = dict(encrypted.HEADERS,
                **({'X-Q4T-Client-Id': OTHER} if other else {}))
        env = {k: v for k, v in os.environ.items() if k not in ('NODE_OPTIONS', 'NODE_PATH', 'LD_PRELOAD')}
        env['NODE_EXTRA_CA_CERTS'] = str(self.out/'tls/ca.pem')
        run = subprocess.run(['node', str(ROOT/'tools/serve_responses/run_files_sdk.mjs')],
            input=json.dumps(command), capture_output=True, text=True, env=env, timeout=20)
        (self.out/(label+'.stderr')).write_text(run.stderr)
        require(not run.stderr and run.stdout, 'Files SDK bridge failed')
        value = json.loads(run.stdout);save(self.out/(label+'.sdk.json'), value)
        require(len(value['exchanges']) == 1 and value['exchanges'][0]['status'] == expected_status,
                'Files status/replay differs: '+label)
        require(value['ok'] == (expected_status == 200) and
                run.returncode == (0 if value['ok'] else 1), 'Files completion differs')
        if value['ok'] and action != 'content':
            name = 'file_list' if action == 'list' else 'file_deleted' if action == 'delete' else 'file_resource'
            require(not errors(value['result'], name), 'Files actual wire schema')
        require(self.edge.error is None and self.edge.process.poll() is None, 'native entry changed')
        self.same_model()
        return value.get('result')

    def business(self):
        document = (ROOT/foreground.DOCUMENT).read_bytes()
        uploaded = self.files('upload-document', 'create', dict(purpose='user_data',
            filename='MODEL.md', data_base64=base64.b64encode(document).decode()))
        binary = bytes(range(256))
        blob = self.files('upload-binary-core', 'create', dict(purpose='evals',
            filename='原字节.bin', data_base64=base64.b64encode(binary).decode()), direct=True)
        require(self.files('metadata-core', 'retrieve', file_id=uploaded['id'], direct=True) == uploaded,
                'actual metadata differs')
        listing = self.files('list-native', 'list', dict(limit=1, order='asc'))
        require(listing['has_more'] and len(listing['data']) == 1, 'Files pagination')
        self.files('foreign-denied', 'retrieve', file_id=uploaded['id'], other=True, expected_status=404)
        check_download(self.files('binary-native', 'content', file_id=blob['id']), binary)
        content = self.files('document-native', 'content', file_id=uploaded['id'])
        check_download(content, document)
        rows = self.plan['requests']
        tool = self.sdk('tool', 'create', rows['tool'])
        calls = [item for item in tool['output'] if item['type'] == 'function_call']
        require(tool['status'] == 'completed' and len(calls) == 1 and
            calls[0]['name'] == 'read_project_record' and calls[0]['status'] == 'completed' and
            json.loads(calls[0]['arguments']) == dict(path=foreground.DOCUMENT, start_line=1, line_count=3),
            'real tool call incomplete')
        result = tool_result(base64.b64decode(content['data_base64']), self.plan['sources'][foreground.DOCUMENT])
        save(self.out/'tool-execution.json', dict(call=calls[0], file_id=uploaded['id'], result=result))
        follow = copy.deepcopy(rows['follow'])
        follow.update(previous_response_id=tool['id'], input=[dict(type='function_call_output',
            call_id=calls[0]['call_id'], output=json.dumps(result, ensure_ascii=False))])
        final = self.sdk('follow', 'create', follow)
        answer = json.loads(foreground.text(final));title = document.decode().splitlines()[0]
        require(final['status'] == 'completed' and answer['title'] in (title, title.lstrip('# ')) and
            answer['citation'] == result['source']['citation'], 'downloaded document tool closure')
        pid, identity = own_object_worker(base.lifecycle.guard.processes(), self.model.pid)
        require(identity['executable'] == self.plan['binary'], 'worker executable mismatch')
        self.history[pid] = identity
        save(self.out/'fault-injection.json', dict(pid=pid, identity=identity,
            mutation='SIGSTOP', model_pid=self.model.pid))
        base.lifecycle.guard.signal_identity(pid, identity, signal.SIGSTOP)
        try:
            self.files('storage-unavailable', 'retrieve', file_id=uploaded['id'], expected_status=503)
            require(self.http('/readyz')[0] == 200, 'storage failure changed core readiness')
            healthy = self.sdk('healthy-during-storage-fault', 'create', rows['healthy'])
            require(healthy['status'] == 'completed' and foreground.text(healthy).strip() == '42',
                    'stateless service failed with auxiliary storage')
        finally:
            current = base.lifecycle.guard.processes().get(pid, {})
            if current.get('start_ticks') == identity['start_ticks']:
                base.lifecycle.guard.signal_identity(pid, identity, signal.SIGCONT)
        # Recovery is an explicit read of the original identity, no upload replay.
        check_download(self.files('document-after-worker-recovery', 'content', file_id=uploaded['id']), document)
        for label, value in [('document', uploaded), ('binary', blob)]:
            self.files('delete-'+label, 'delete', file_id=value['id'])
        require(not self.files('empty-list', 'list')['data'], 'deleted Files still listed')
        self.accounting('final-accounting', [tool, final, healthy])
        self.same_model()
        save(self.out/'business-checks.json', dict(actual_sdk_files=True, native_tls=True,
            direct_core=True, binary_exact=True, downloaded_real_document_tool_closure=True,
            storage_failure_same_model_sse=True, storage_recovery_preserves_file=True,
            exact_generation_usage=True, generations=3, file_input_resolution=False,
            native_parent_admission='test adapter tied to actual model identity', performance_claim=False))

    def cleanup(self):
        try:
            if self.edge:self.edge.close()
        finally:super().cleanup()
        background.probe_ports((self.plan['edge_port'],))
        value = read(self.out/'cleanup.json');value['native_port_released'] = True
        save(self.out/'cleanup.json', value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    command = modes.add_parser('prepare')
    command.add_argument('--binary', type=Path, required=True)
    command.add_argument('--ingress', type=Path, required=True)
    command.add_argument('--output', type=Path, required=True)
    command.add_argument('--port', type=int, default=19011)
    command = modes.add_parser('run');command.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'prepare':prepare(args)
    else:
        def timeout(_signal, _frame):raise TimeoutError('Files business deadline')
        prior = signal.signal(signal.SIGALRM, timeout)
        try:
            signal.setitimer(signal.ITIMER_REAL, LIMITS['seconds']-40)
            with base.lifecycle.child_subreaper():Stage(read(args.plan)).run()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0);signal.signal(signal.SIGALRM, prior)


if __name__ == '__main__':main()

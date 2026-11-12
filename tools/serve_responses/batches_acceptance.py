"""Native Batch business and compatible rollback: 2 starts/8 generations.

At most10 model steps/1200s in one strict lease. No automatic model replay,
no full model/performance matrix, no change to the deployed service/default.
"""
import argparse
import copy
import hashlib
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import time
import uuid

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from jsonschema import Draft202012Validator
from tools.serve_completions.acceptance import raw_prompt
from tools.serve_responses import encryption_acceptance as encrypted
from tools.serve_responses import background_acceptance as background
from tools.serve_responses import acceptance as foreground
from tools.serve_responses.file_preparation_acceptance import model_steps
from tools.serve_responses.test_contract import errors
from tools.serve_recovery import ledger, quota_control, usage_policy
from tools.serve_service.package import read, require, save, sha

LIMITS = dict(starts=2, chats=8, controls=2000, seconds=1200)
MAX_MODEL_STEPS = 10
ROLLBACK_COMMIT = 'fc69f823'
TERMINAL = {'completed', 'failed', 'cancelled', 'expired'}


def requests():
    original = foreground.requests()
    chat = original['chat']
    schema = foreground.base.closed(dict(values=dict(type='array', minItems=512,
        maxItems=512, items=dict(type='boolean', const=True))))
    truncated = dict(chat, max_completion_tokens=1,
        messages=[dict(role='user', content='Return 512 true values in values.')],
        response_format=dict(type='json_schema', json_schema=dict(name='bounded',
            strict=True, schema=schema)))
    tool = dict(original['tool'], background=True, store=True, stream=False)
    tool.pop('stream_options')
    cancel = dict(original['plain'], background=True, store=True,
        max_output_tokens=1536, input='Return exactly 512 true values in values.',
        text=dict(format=dict(type='json_schema', name='cancel', strict=True,
                              schema=schema)))
    return dict(chat=[chat, truncated], responses=[tool, original['plain']],
        completions=[dict(model=foreground.MODEL, temperature=0, max_tokens=32,
            prompt=raw_prompt('Compute 17 + 25. Reply only with the decimal number.'))],
        follow=dict(original['follow'], stream=True,
            stream_options=dict(include_obfuscation=False)), cancel=[cancel],
        rollback=[chat])


def jsonl(raw, expected):
    require(raw.endswith(b'\n'), 'result lost final whole-line delimiter')
    lines = [json.loads(line) for line in raw.splitlines()]
    require([row['custom_id'] for row in lines] == expected,
            'result order/identity/count differs')
    require(all(row.get('id') and row.get('response') and row.get('error') is None
                for row in lines), 'original endpoint receipt missing')
    return lines


def usage(value):
    require(type(value) is dict, 'actual usage is unknown')
    keys = ('input_tokens', 'output_tokens', 'total_tokens')
    require(all(type(value.get(k)) is int and value[k] >= 0 for k in keys),
            'invalid usage values')
    require(value['total_tokens'] == value['input_tokens'] + value['output_tokens'],
            'actual usage sum differs')
    require(0 <= value['input_tokens_details']['cached_tokens'] <= value['input_tokens'] and
        0 <= value['output_tokens_details']['reasoning_tokens'] <= value['output_tokens'],
        'invalid actual usage detail')
    return value


def check_batch(value, total, completed, failed, status='completed'):
    require(not errors(value, 'batch_resource'), 'Batch schema failure')
    require(value['status'] == status and value['request_counts'] ==
        dict(total=total, completed=completed, failed=failed), 'Batch terminal/counts')
    return usage(value.get('usage'))


def check_failure_usage(batch, success):
    value = usage(batch['usage'])
    known = success['usage']
    require(value['input_tokens'] > known['prompt_tokens'] and
            value['output_tokens'] == known['completion_tokens'] + 1,
            'truncated actual input/one accepted output not included')


def prepare(args):
    source = args.rollback_source.resolve(strict=True)
    binary = args.rollback_binary.resolve(strict=True)
    revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'],
                                       text=True).strip()
    require(revision.startswith(ROLLBACK_COMMIT) and not subprocess.check_output(
        ['git', '-C', str(source), 'status', '--porcelain']), 'rollback source drift')
    cache = binary.parent/'CMakeCache.txt'
    require(('CMAKE_HOME_DIRECTORY:INTERNAL='+str(source)) in cache.read_text().splitlines(),
            'rollback build source mismatch')
    background.prepare(args, rows=requests(), limits=LIMITS, scope=__doc__)
    path = args.output.resolve()/'plan.json'
    plan = read(path)
    for name in ['tools/serve_completions/acceptance.py',
                 'tools/serve_responses/batches_acceptance.py',
                 'tools/serve_responses/test_batches_acceptance.py']:
        plan['sources'][name] = sha(ROOT/name)
    plan['rollback'] = dict(source=str(source), source_commit=revision, binary=str(binary),
        sha256=sha(binary), cmake_cache_sha256=sha(cache))
    plan['service_id'] = str(uuid.uuid4())
    plan['planned'] = dict(starts=2, generations=8, model_steps_max=10,
        candidate_batch_rows=6, candidate_foreground=1, rollback_batch_rows=1)
    plan['mutations'] = ['isolated persistent directory/client policy',
        'cancel own running Batch after observing its background executor',
        'native same-parent minute quota freeze/restore, both domains',
        'explicit graceful candidate->compatible rollback transition; no generation retry']
    plan['derived_requests'] = ['Batch/Files/Response IDs come from actual API results',
        'tool input is actual docs/MODEL.md lines1-3; SSE follows original Response',
        'single token structural failure must preserve its committed native usage',
        'actual snapshot and receipt cross versions; naturally expired windows remain expired']
    save(path, plan)


class Stage(encrypted.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_BATCH_BUSINESS_AND_COMPATIBLE_ROLLBACK'
    requests_per_minute = 24
    tokens_per_minute = 32768

    def client_policy(self):
        policy = super().client_policy()
        policy['clients'][0]['max_output_tokens'] = 1536
        return policy

    def extra_model_args(self):
        return ['--object-max-client-mib', '256', '--object-max-total-mib', '512']

    def start(self):
        generation = self.counts['starts'] + 1
        parent = foreground.base.lifecycle.guard.processes()[os.getpid()]
        added = dict(Q4T_POLICY_PARENT_PID=str(os.getpid()),
            Q4T_POLICY_PARENT_START=str(parent['start_ticks']),
            Q4T_MODEL_GENERATION=str(generation), Q4T_SERVICE_ID=self.plan['service_id'])
        prior = {key: os.environ.get(key) for key in added}
        original = self.plan
        if generation == 2:
            rollback = original['rollback']
            require(sha(rollback['binary']) == rollback['sha256'], 'rollback binary drift')
            self.plan = dict(original, binary=rollback['binary'], binary_sha256=rollback['sha256'])
        try:
            os.environ.update(added)
            super().start()
        finally:
            self.plan = original
            for key, value in prior.items():
                if value is None: os.environ.pop(key, None)
                else: os.environ[key] = value
        config = read(self.out/f'config-{generation}.json')
        Draft202012Validator(read(ROOT/'docs/serve-config-v1.schema.json')).validate(config)
        if generation == 1:
            require(all(row['state'] == 'ready' and row['attempts'] == 1 for row in
                        config['application_startup'].values()), 'native owners did not initialize')
        restored = self.admin('usage_restore', getattr(self, 'snapshot', None))
        raw = ledger.canonical(getattr(self, 'snapshot', None))
        require(restored['usage']['receipt_sha256'] == hashlib.sha256(raw).hexdigest(),
                'usage restoration receipt mismatch')

    def admin(self, operation, snapshot=None):
        self.budget('controls')
        self.same_model()
        address = quota_control.address(self.plan['service_id'], self.counts['starts'])
        raw = ledger.canonical(snapshot) if operation == 'usage_restore' else None
        command = dict(op=operation)
        if raw is not None: command.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as sock:
            sock.settimeout(3)
            sock.connect('\0'+address)
            peer = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            require(peer[0] == self.model.pid and peer[1] == os.geteuid(), 'usage peer mismatch')
            sock.sendall(ledger.canonical(command))
            if raw is not None:
                for offset in range(0, len(raw), 16384): sock.sendall(raw[offset:offset+16384])
            reply = json.loads(sock.recv(65536))
        save(self.out/f"usage-{self.counts['starts']}-{operation}.json", reply)
        require(reply['ok'], 'native usage operation failed: '+str(reply))
        if operation == 'usage_freeze': usage_policy.snapshot(reply['usage']['snapshot'])
        return reply

    def request(self, method, path, body=None, *, label, content_type='application/json'):
        self.budget('controls')
        connection = http.client.HTTPConnection('127.0.0.1', self.plan['port'], timeout=120)
        headers = dict(encrypted.HEADERS)
        if body is not None: headers['Content-Type'] = content_type
        started = time.monotonic()
        try:
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            raw = response.read(4*1024**2+1)
            require(len(raw) <= 4*1024**2, 'response capture bound')
            (self.out/(label+'.wire')).write_bytes(raw)
            save(self.out/(label+'.http.json'), dict(status=response.status,
                headers=dict(response.getheaders()), seconds=time.monotonic()-started))
            require(response.status == 200, label+': '+raw[:512].decode(errors='replace'))
            self.same_model()
            return raw
        finally: connection.close()

    def create(self, label, endpoint, rows):
        for _ in rows: self.budget('chats')
        lines = [dict(custom_id=label+'-'+str(i), method='POST', url=endpoint, body=row)
                 for i, row in enumerate(rows)]
        text = ''.join(json.dumps(line, separators=(',', ':'))+'\n' for line in lines)
        (self.out/(label+'-input.jsonl')).write_text(text)
        wire = ('--b\r\nContent-Disposition: form-data; name="purpose"\r\n\r\nbatch'
            '\r\n--b\r\nContent-Disposition: form-data; name="file"; filename="input.jsonl"'
            '\r\n\r\n'+text+'\r\n--b--\r\n').encode()
        file = json.loads(self.request('POST', '/v1/files', wire, label=label+'-upload',
                                      content_type='multipart/form-data; boundary=b'))
        body = dict(input_file_id=file['id'], endpoint=endpoint, completion_window='24h',
            output_expires_after=dict(anchor='created_at', seconds=3600))
        value = json.loads(self.request('POST', '/v1/batches', ledger.canonical(body),
                                       label=label+'-create'))
        require(value['status'] == 'validating', 'Batch did not accept its original task')
        return value

    def finish(self, label, created):
        end = time.monotonic()+180
        for i in range(1000):
            require(time.monotonic() < end, 'original Batch terminal timeout')
            value = json.loads(self.request('GET', '/v1/batches/'+created['id'],
                                           label=label+'-poll-'+str(i)))
            if value['status'] in TERMINAL:
                require(value['expires_at'] == created['expires_at'], 'Batch deadline changed')
                self.drained(label+'-drain')
                return value
            time.sleep(.125)
        raise TimeoutError('Batch observation count exhausted')

    def outputs(self, label, batch, expected, key='output_file_id'):
        raw = self.request('GET', '/v1/files/'+batch[key]+'/content', label=label)
        return jsonl(raw, expected)

    def batch_idle(self, label):
        end = time.monotonic()+5
        while time.monotonic() < end:
            status, raw = self.http('/v1/config')
            require(status == 200, 'config unavailable')
            config = json.loads(raw)
            if config['batches']['owned'] == 0 and config['responses_background']['owned'] == 0:
                require(config['batches']['reserved_result_bytes'] == 0, 'Batch result lease leaked')
                save(self.out/(label+'-config.json'), config)
                return self.drained(label)
            time.sleep(.125)
        raise TimeoutError('original Batch/Response owners did not drain')

    def business(self):
        rows = self.plan['requests']
        retained = []
        chat = self.finish('chat', self.create('chat', '/v1/chat/completions', rows['chat']))
        check_batch(chat, 2, 1, 1)
        chat_output = self.outputs('chat-output', chat, ['chat-0'])[0]['response']['body']
        require(chat_output['choices'][0]['message']['content'].strip() == '42', 'Chat answer')
        failure = self.outputs('chat-error', chat, ['chat-1'], 'error_file_id')[0]['response']
        require(failure['status_code'] == 502 and
            failure['body']['error']['code'] == 'structured_output_incomplete', 'wrong bounded failure')
        check_failure_usage(chat, chat_output)
        retained.append(chat)
        responses = self.finish('responses', self.create('responses', '/v1/responses', rows['responses']))
        check_batch(responses, 2, 2, 0)
        outputs = self.outputs('responses-output', responses, ['responses-0', 'responses-1'])
        tool, plain = [row['response']['body'] for row in outputs]
        background.check_response(tool, 'completed')
        background.check_response(plain, 'completed')
        require(foreground.text(plain).strip() == '42', 'Response answer')
        calls = [item for item in tool['output'] if item['type'] == 'function_call']
        require(len(calls) == 1 and calls[0]['name'] == 'read_project_record', 'original tool call')
        arguments = json.loads(calls[0]['arguments'])
        require(arguments == dict(path=foreground.DOCUMENT, start_line=1, line_count=3), 'tool args')
        actual = foreground.base.ProjectRecords(ROOT).read(arguments)
        require(actual['ok'] and actual['source']['sha256'] == self.plan['sources'][foreground.DOCUMENT],
                'real tool source changed')
        save(self.out/'actual-tool.json', dict(call=calls[0], result=actual))
        follow = copy.deepcopy(rows['follow'])
        follow.update(previous_response_id=tool['id'], input=[dict(type='function_call_output',
            call_id=calls[0]['call_id'], output=json.dumps(actual, ensure_ascii=False))])
        final = self.sdk('follow-sse', 'create', follow)
        background.check_response(final, 'completed')
        answer = json.loads(foreground.text(final))
        title = actual['source']['text'].splitlines()[0].split(': ', 1)[1]
        require(answer['title'] in (title, title.lstrip('# ')) and
                answer['citation'] == actual['source']['citation'], 'tool closure answer/citation')
        retained.append(responses)
        completions = self.finish('completion', self.create('completion', '/v1/completions', rows['completions']))
        check_batch(completions, 1, 1, 0)
        completion = self.outputs('completion-output', completions, ['completion-0'])[0]['response']['body']
        require(completion['choices'][0]['text'].strip() == '42', 'raw Completions answer')
        retained.append(completions)
        pending = self.create('cancel', '/v1/responses', rows['cancel'])
        end = time.monotonic()+30
        while time.monotonic() < end:
            status, raw = self.http('/v1/config')
            require(status == 200, 'cancellation observation unavailable')
            if json.loads(raw)['responses_background']['running'] == 1: break
            time.sleep(.125)
        else: raise TimeoutError('original cancellation owner never ran')
        time.sleep(.3)
        self.request('POST', '/v1/batches/'+pending['id']+'/cancel', b'{}', label='cancel-request')
        cancelled = self.finish('cancel', pending)
        check_batch(cancelled, 1, 1, 0, 'cancelled')
        cancelled_response = self.outputs('cancel-output', cancelled, ['cancel-0'])[0]['response']['body']
        require(cancelled_response['status'] == 'cancelled' and
                usage(cancelled['usage'])['input_tokens'] > 0, 'cancel lost original actual work')
        retained.append(cancelled)
        metrics = self.batch_idle('candidate-final')
        key = '{client="'+encrypted.CLIENT+'"}'
        total = sum(v['usage']['total_tokens'] for v in retained)
        require(metrics['q4t_client_batch_rate_settled_tokens_total'+key] == total and
                metrics['q4t_client_batch_rate_reserved_tokens'+key] == 0,
                'Batch actual usage/rate settlement differs')
        require(metrics['q4t_client_rate_settled_tokens_total'+key] == final['usage']['total_tokens'],
                'online and Batch minute pools mixed')
        frozen = self.admin('usage_freeze')['usage']['snapshot']
        require(frozen['version'] == 2 and int(frozen['clients'][0]['batch']['settled_tokens']) == total,
                'both quota domains not sealed')
        self.snapshot = frozen
        self.cleanup()
        save(self.out/'candidate-cleanup.json', read(self.out/'cleanup.json'))
        require(self.model.returncode == 0, 'candidate graceful cleanup failed')
        self.model = None
        self.start()
        for i, batch in enumerate(retained):
            restored = json.loads(self.request('GET', '/v1/batches/'+batch['id'], label=f'rollback-batch-{i}'))
            require(restored == batch, 'Batch original receipt changed across rollback')
            for key in ('output_file_id', 'error_file_id'):
                if batch.get(key):
                    raw = self.request('GET', '/v1/files/'+batch[key]+'/content', label=f'rollback-file-{i}-{key}')
                    original_label = ['chat-output' if key=='output_file_id' else 'chat-error',
                                      'responses-output', 'completion-output', 'cancel-output'][i]
                    require(raw == (self.out/(original_label+'.wire')).read_bytes(), 'original JSONL changed')
        restored_tool = json.loads(self.request('GET', '/v1/responses/'+tool['id'], label='rollback-original-response'))
        require(restored_tool['status'] == 'completed' and restored_tool['usage'] == tool['usage'] and
                restored_tool['output'] == tool['output'], 'original background result changed')
        fresh = self.finish('rollback', self.create('rollback', '/v1/chat/completions', rows['rollback']))
        check_batch(fresh, 1, 1, 0)
        output = self.outputs('rollback-output', fresh, ['rollback-0'])[0]['response']['body']
        require(output['choices'][0]['message']['content'].strip() == '42', 'rollback execution failed')
        metrics = self.batch_idle('rollback-final')
        restored = self.admin('usage_freeze')['usage']['snapshot']
        require(int(restored['clients'][0]['batch']['settled_tokens']) == total+fresh['usage']['total_tokens'] and
                restored['clients'][0]['settled_tokens'] == frozen['clients'][0]['settled_tokens'],
                'cross-version quota continuity failed')
        steps = sum(model_steps((self.out/f'model-{i}.log').read_text()) for i in (1, 2))
        require(steps == 8 and steps <= MAX_MODEL_STEPS, 'model step count/replay')
        save(self.out/'business-checks.json', dict(generations=8, model_steps=steps,
            actual_tools=True, failed_usage=True, cancelled_usage=True,
            original_background_terminal=True, cross_version_results=True,
            dual_quota_restore=True, default_unchanged=True, performance_claim=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    p = modes.add_parser('prepare')
    for name in ['binary', 'rollback-binary', 'rollback-source', 'output']:
        p.add_argument('--'+name, type=Path, required=True)
    p.add_argument('--port', type=int, default=19051)
    p = modes.add_parser('run'); p.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'prepare': prepare(args)
    else:
        def timeout(_signal, _frame): raise TimeoutError('Batch business deadline')
        prior = signal.signal(signal.SIGALRM, timeout)
        try:
            signal.setitimer(signal.ITIMER_REAL, LIMITS['seconds']-40)
            with foreground.base.lifecycle.child_subreaper(): Stage(read(args.plan)).run()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, prior)


if __name__ == '__main__': main()

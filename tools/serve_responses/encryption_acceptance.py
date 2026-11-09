"""Actual encrypted state, live key rotation and restart; bounded native business."""
import argparse
import base64
import copy
import http.client
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_responses import acceptance as foreground
from tools.serve_responses import background_acceptance as background
from tools.serve_responses import summary_acceptance as summary
from tools.serve_responses.test_contract import errors
from tools.serve_service.package import read, require, save, sha

LIMITS = dict(starts=2, chats=8, controls=2000, seconds=900)
MAX_MODEL_STEPS = 8
CLIENT = 'encryption-test'
HEADERS = {'X-Q4T-Client-Id': CLIENT, 'X-Q4T-Gateway-Token': 'b' * 64}


def requests():
    old = foreground.requests()
    common = dict(model=foreground.MODEL, temperature=0, store=False,
        reasoning=dict(effort='low'), max_output_tokens=384,
        include=['reasoning.encrypted_content'])
    continuation = dict(common, input=[], stream=True,
        stream_options=dict(include_obfuscation=False))
    return dict(
        initial=dict(common, input=old['plain']['input'],
                     reasoning=dict(effort='low', summary='concise')),
        continuation=continuation,
        tool=dict(old['tool'], **{k: v for k, v in common.items() if k != 'model'}),
        follow=dict(old['follow'], **{k: v for k, v in common.items() if k != 'model'}),
        background=dict(common, input=old['plain']['input'], store=True,
                        background=True, stream=True,
                        stream_options=dict(include_obfuscation=False)),
        restarted=copy.deepcopy(continuation),
        retired=dict(common, input=[]), chat=old['chat'])


def encrypted_items(value, expected_key=None):
    background.check_response(value, 'completed')
    items = [item for item in value['output'] if item['type'] == 'reasoning']
    require(items, 'actual reasoning state absent')
    for item in items:
        encoded = item.get('encrypted_content', '')
        require(re.fullmatch(r'q4t1:[0-9a-f]{32}:[0-9a-f]{24}:[0-9a-f]{32}:(?:[0-9a-f]{2})+',
                             encoded), 'invalid authenticated state envelope')
        if expected_key is not None:
            require(encoded.split(':')[1] == expected_key, 'state used wrong key generation')
    require(len({item['encrypted_content'] for item in items}) == len(items),
            'ciphertext reused across reasoning items')
    return items


def opaque_output(value):
    encrypted_items(value)
    items = copy.deepcopy(value['output'])
    for item in items:
        if item['type'] == 'reasoning':
            item.pop('content', None)
            item['summary'] = []
    return items


def continuation(template, value):
    body = copy.deepcopy(template)
    body['input'] = opaque_output(value) + [dict(role='user', content=
        'What was the result of the preceding calculation? Reply only with the decimal number.')]
    return body


def check_done_events(result):
    events = result.get('chunks') or []
    require(events, 'actual SSE events missing')
    done = [e['item'] for e in events if e['type'] == 'response.output_item.done']
    require(done == result['result']['output'], 'done item differs from terminal encrypted state')
    return len(events)


def key_receipt(receipt):
    require(receipt['status'] == 200 and not receipt['commit_unknown'],
            'key command did not return a committed receipt')
    value = receipt['value']
    require(set(value) == {'version', 'revision', 'active', 'keys', 'max_keys',
                           'max_seals_per_key'}, 'key receipt leaked or lost fields')
    require(value['revision'] >= 1 and value['keys'] and
            all(set(k) == {'id', 'created_at', 'uses'} for k in value['keys']) and
            value['active'] in {k['id'] for k in value['keys']}, 'invalid public key descriptor')
    return value


def prepare(args):
    background.prepare(args, rows=requests(), limits=LIMITS, scope=__doc__)
    path = args.output.resolve() / 'plan.json'
    plan = read(path)
    plan['planned'] = dict(starts=2, chats=8, positive_generations=7,
                           negative_creates=1, internal_model_steps=MAX_MODEL_STEPS)
    plan['mutations'] = [
        'rotate keys online through actual same-UID native CLI, retain old key',
        'gracefully stop only owned model after draining, restart once with same private object directory',
        'explicitly retire first key online; reject its old ciphertext without model execution']
    plan['derived_requests'] = [
        'manual input copies actual output, removes plaintext reasoning/summary and omits origin input',
        'tool output is actual docs/MODEL.md lines1-3; no synthetic result',
        'inspect revision and key IDs come from actual CLI receipts; no mutation retry',
        'stored background replay reuses original SSE; no repeated model generation',
        'per-process usage is checked separately; no claim of direct-engine RPM persistence',
        'old accepted default package and state remain protected']
    save(path, plan)


class Stage(foreground.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_RESPONSES_ENCRYPTION_BUSINESS'
    requests_per_minute = 8
    tokens_per_minute = 32768

    def http(self, path, body=None, *, label=None, timeout=30):
        if body is None:
            self.budget('controls')
        headers = {} if path in ('/readyz', '/metrics', '/v1/config') else dict(HEADERS)
        if body is not None:
            headers['Content-Type'] = 'application/json'
        conn = http.client.HTTPConnection('127.0.0.1', self.plan['port'], timeout=timeout)
        started = time.monotonic()
        try:
            conn.request('GET' if body is None else 'POST', path,
                body=None if body is None else json.dumps(body, ensure_ascii=False).encode(),
                headers=headers)
            response = conn.getresponse()
            raw = response.read(4 * 1024 * 1024 + 1)
            require(len(raw) <= 4 * 1024 * 1024, 'response size bound')
            if label:
                (self.out / (label + '.wire')).write_bytes(raw)
                save(self.out / (label + '.http.json'), dict(status=response.status,
                    headers=dict(response.getheaders()), seconds=time.monotonic()-started))
            return response.status, raw
        finally:
            conn.close()

    def client_policy(self):
        return dict(version=3, revision=0, fair_queue=True,
            gateway_token=HEADERS['X-Q4T-Gateway-Token'], clients=[dict(id=CLIENT,
                max_inflight=4, max_control_inflight=4, max_prompt_tokens=16384,
                max_output_tokens=512, max_body_bytes=1048576,
                requests_per_minute=self.requests_per_minute,
                tokens_per_minute=self.tokens_per_minute)])

    def start(self):
        background.probe_ports((self.plan['port'], self.plan['operations_port']))
        generation = self.counts['starts'] + 1
        policy = self.client_policy()
        save(self.out / 'client-policy.json', policy)
        args = [self.plan['binary'], 'serve', '--model-dir', self.plan['model'],
            '--host', '127.0.0.1', '--port', str(self.plan['port']),
            '--operations-port', str(self.plan['operations_port']), '--mtp',
            '--max-seq', '1', '--max-len', '8192', '--max-prefill', '2048',
            '--max-tokens', '384', '--max-inflight-requests', '8',
            '--max-queued-requests', '2', '--object-store-dir', str(self.out/'objects'),
            '--client-policy-file', str(self.out/'client-policy.json')]
        save(self.out / f'model-{generation}-command.json', args)
        self.budget('starts')
        with (self.out / f'model-{generation}.log').open('xb', buffering=0) as log:
            self.model = subprocess.Popen(args, cwd=ROOT, stdout=log,
                stderr=subprocess.STDOUT, pass_fds=(self.lease,))
        self.model_identity = foreground.base.lifecycle.guard.processes()[self.model.pid]
        self.history[self.model.pid] = self.model_identity
        save(self.out / f'model-{generation}-identity.json',
             dict(pid=self.model.pid, **self.model_identity))
        end = time.monotonic() + 180
        while time.monotonic() < end:
            self.same_model()
            try:
                if self.http('/readyz', timeout=1)[0] == 200:
                    break
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(.1)
        else:
            raise TimeoutError('model readiness')
        for name in ('capabilities', 'config'):
            status, raw = self.http('/v1/' + name, label=f'initial-{generation}-' + name)
            require(status == 200, name + ' unavailable')
            save(self.out / f'{name}-{generation}.json', json.loads(raw))
        caps = read(self.out / f'capabilities-{generation}.json')
        require(caps['responses']['encrypted_reasoning']['enabled'], 'crypto capability absent')
        config = read(self.out / f'config-{generation}.json')['application_state_health']
        require(config['key_admin_ready'] and not config['key_admin_readiness_dependency'],
                'key administration unavailable or core health dependent')
        self.client.protocol_sha256 = caps['protocol_source_sha256']

    def sdk(self, label, action, request=None, *, response_id=None, query=None,
            expected_status=None, headers=None, drain=True):
        self.budget('chats' if action == 'create' else 'controls')
        command = dict(action=action, request=request, id=response_id, query=query,
            base_url=f"http://127.0.0.1:{self.plan['port']}/v1",
            headers=HEADERS if headers is None else headers,
            sdk_directory=self.plan['sdk']['sdk_directory'], timeout_ms=120000)
        save(self.out / (label + '.request.json'), command)
        env = {k: v for k, v in os.environ.items() if k not in
               ('NODE_OPTIONS', 'NODE_PATH', 'LD_PRELOAD')}
        run = subprocess.run(['node', str(ROOT/'tools/serve_responses/run_sdk.mjs')],
            input=json.dumps(command, ensure_ascii=False), capture_output=True,
            text=True, env=env, timeout=135)
        (self.out / (label + '.stderr.log')).write_text(run.stderr)
        require(not run.stderr and run.stdout, 'SDK bridge failed')
        result = json.loads(run.stdout)
        (self.out / (label + '.wire')).write_bytes(
            base64.b64decode(result.pop('wire_base64'), validate=True))
        save(self.out / (label + '.sdk.json'), result)
        require(len(result['exchanges']) == 1, 'SDK request replay')
        if expected_status is not None:
            require(not result['ok'] and result['error']['status'] == expected_status,
                    'expected protocol rejection absent')
        else:
            require(run.returncode == 0 and result['ok'],
                    'SDK failed: ' + label + ' ' + str(result.get('error')))
            for event in result.get('chunks') or []:
                require(not errors(event, 'response_event'), 'event schema: ' + event['type'])
            if result['result'].get('object') == 'response':
                background.check_response(result['result'])
            if result.get('chunks'):
                check_done_events(result)
        self.same_model()
        if drain:
            self.drained(label + '-drain')
        return result.get('result')

    def keys(self, label, action='inspect', *, revision=None, retire=()):
        self.budget('controls')
        self.same_model()
        command = [self.plan['binary'], 'state-keys', action,
                   '--object-store-dir', str(self.out / 'objects')]
        if revision is not None:
            command += ['--expected-revision', str(revision)]
        for key in retire:
            command += ['--retire', key]
        run = subprocess.run(command, capture_output=True, text=True, timeout=10)
        save(self.out / (label + '.cli.json'), dict(command=command,
             returncode=run.returncode, stdout=run.stdout, stderr=run.stderr))
        require(run.returncode == 0 and not run.stderr, 'native key CLI failed: ' + label)
        self.same_model()
        return key_receipt(json.loads(run.stdout))

    def accounting(self, label, values, *, chat=None, rejected=0):
        metrics = self.drained(label)
        expected_input = sum(v['usage']['input_tokens'] for v in values)
        expected_output = sum(v['usage']['output_tokens'] for v in values)
        count = len(values)
        if chat:
            expected_input += chat['usage']['prompt_tokens']
            expected_output += chat['usage']['completion_tokens']
            count += 1
        key = '{client="' + CLIENT + '"}'
        require(metrics['q4t_requests_total'] == count + rejected and
                metrics['q4t_requests_success_total'] == count and
                metrics['q4t_requests_error_total'] == rejected,
                'unexpected generation handler request/success/error accounting')
        require(metrics['q4t_client_rate_admitted_total' + key] == count and
                metrics['q4t_client_rate_reserved_tokens' + key] == 0 and
                metrics['q4t_client_rate_settled_tokens_total' + key] ==
                expected_input + expected_output, 'client accounting differs from actual usage')
        require(metrics['q4t_prompt_tokens_total'] == expected_input and
                metrics['q4t_generation_tokens_total'] == expected_output,
                'actual input/output usage mismatch')

    def cleanup(self):
        super().cleanup()
        for name, digest in self.plan['protected'].items():
            require(sha(name) == digest, 'default changed during encrypted state acceptance')

    def business(self):
        rows = self.plan['requests']
        first = self.sdk('initial', 'create', rows['initial'])
        summary.check_summary(first, arithmetic=True)
        original = self.keys('initial-keys')
        old_key = original['active']
        encrypted_items(first, old_key)
        self.sdk('stateless-absent', 'retrieve', response_id=first['id'], expected_status=404)
        restored = self.sdk('continuation', 'create', continuation(rows['continuation'], first))
        require(foreground.text(restored).strip() == '42', 'stateless context not restored')
        encrypted_items(restored, old_key)
        tool = self.sdk('tool', 'create', rows['tool'])
        encrypted_items(tool, old_key)
        calls = [item for item in tool['output'] if item['type'] == 'function_call']
        require(len(calls) == 1 and calls[0]['name'] == 'read_project_record' and
                calls[0]['status'] == 'completed', 'completed actual tool call missing')
        arguments = json.loads(calls[0]['arguments'])
        require(arguments == dict(path=foreground.DOCUMENT, start_line=1, line_count=3),
                'actual tool arguments')
        result = foreground.base.ProjectRecords(ROOT).read(arguments)
        require(result['ok'] and result['source']['sha256'] ==
                self.plan['sources'][foreground.DOCUMENT], 'document identity')
        save(self.out / 'tool-execution.json', dict(call=calls[0], result=result))
        before = self.keys('before-rotate')
        rotated = self.keys('rotate-retain', 'rotate', revision=before['revision'])
        require(rotated['active'] != old_key and
                {k['id'] for k in rotated['keys']} == {old_key, rotated['active']} and
                rotated['revision'] == before['revision'] + 1, 'rotation receipt')
        follow = copy.deepcopy(rows['follow'])
        follow['input'] = opaque_output(tool) + [dict(type='function_call_output',
            call_id=calls[0]['call_id'], output=json.dumps(result, ensure_ascii=False))]
        answer = self.sdk('follow', 'create', follow)
        encrypted_items(answer, rotated['active'])
        actual = json.loads(foreground.text(answer))
        title = result['source']['text'].splitlines()[0].split(': ', 1)[1]
        require(actual['title'] in (title, title.lstrip('# ')) and
                actual['citation'] == result['source']['citation'], 'document closure failed')
        stored = self.sdk('background', 'create', rows['background'])
        encrypted_items(stored, rotated['active'])
        require(foreground.text(stored).strip() == '42', 'background arithmetic')
        before_restart = self.keys('before-restart')
        self.accounting('first-process', [first, restored, tool, answer, stored])
        self.cleanup()
        receipt = read(self.out / 'cleanup.json')
        save(self.out / 'first-cleanup.json', receipt)
        require(receipt['model_exit'] == 0, 'first process did not gracefully stop')
        self.model = None
        self.start()
        require(self.keys('after-restart') == before_restart, 'persisted keys/counters changed')
        retrieved = self.sdk('stored-get', 'retrieve', response_id=stored['id'],
                             query=dict(include=['reasoning.encrypted_content']))
        require({k: v for k, v in retrieved.items() if k != 'output_text'} == stored,
                'stored encrypted state changed across restart')
        replay = self.sdk('stored-replay', 'retrieve', response_id=stored['id'],
            query=dict(stream=True, include_obfuscation=False,
                       include=['reasoning.encrypted_content']))
        require(replay == stored, 'original ciphertext or events changed on replay')
        resumed = self.sdk('restarted', 'create', continuation(rows['restarted'], stored))
        require(foreground.text(resumed).strip() == '42', 'encrypted state after restart')
        encrypted_items(resumed, rotated['active'])
        before_retire = self.keys('before-retire')
        retired = self.keys('retire-old', 'rotate', revision=before_retire['revision'],
                            retire=[old_key])
        require(old_key not in {k['id'] for k in retired['keys']} and
                rotated['active'] in {k['id'] for k in retired['keys']}, 'explicit retirement')
        bad = continuation(rows['retired'], first)
        self.sdk('retired-rejected', 'create', bad, expected_status=400)
        error = read(self.out / 'retired-rejected.sdk.json')['error']
        require(error['code'] == 'invalid_encrypted_content', 'wrong retired-state error')
        self.budget('chats')
        status, raw = self.http('/v1/chat/completions', rows['chat'], label='same-pid-chat')
        chat = json.loads(raw)
        require(status == 200 and chat['choices'][0]['finish_reason'] == 'stop' and
                chat['choices'][0]['message']['content'].strip() == '42', 'ordinary Chat failed')
        self.accounting('second-process', [resumed], chat=chat, rejected=1)
        logs = ''.join((self.out / f'model-{i}.log').read_text() for i in (1, 2))
        for response, steps in [(first, 2), (restored, 1), (tool, 1), (answer, 1),
                                (stored, 1), (resumed, 1)]:
            summary.check_steps(logs, response, steps)
        require(len(re.findall(r'^\[sampling\] request=', logs, re.M)) == MAX_MODEL_STEPS,
                'internal model execution budget or replay')
        require('[mtp-accept] request=' + chat['id'] in logs, 'ordinary Chat did not exercise MTP')
        save(self.out / 'business-checks.json', dict(external_create_attempts=8,
            positive_generations=7, internal_model_steps=MAX_MODEL_STEPS,
            actual_tool_executions=1, omitted_plaintext_and_origin=True,
            live_rotation_no_model_restart=True, explicit_retirement=True,
            persisted_restart=True, original_sse_replay=True,
            per_process_actual_usage=True, direct_engine_rpm_persistence_claim=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    command = modes.add_parser('prepare')
    command.add_argument('--binary', type=Path, required=True)
    command.add_argument('--output', type=Path, required=True)
    command.add_argument('--port', type=int, default=18985)
    command = modes.add_parser('run')
    command.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'prepare':
        prepare(args)
    else:
        with foreground.base.lifecycle.child_subreaper():
            Stage(read(args.plan)).run()


if __name__ == '__main__':
    main()

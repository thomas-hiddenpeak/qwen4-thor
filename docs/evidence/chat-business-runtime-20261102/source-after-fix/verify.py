"""Bounded real HTTP protocol checks. Run only inside gpu_exclusive.py.

Does not stop other services or install a binary. Only the demo's explicitly
allowlisted local read-only lookup can execute; no arbitrary model dispatch.
All requests and raw responses are retained, including the first failure.
"""
import argparse
import copy
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import subprocess
import secrets
import sys
import time
from unittest.mock import patch
import urllib.request

from acceptance import LIMITS

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / 'tools/verify/startup_delivery'))
from deploy import require_guard
sys.path.insert(0, str(ROOT / 'tools/examples'))
import agent_tools_demo as demo
from chat_client import (ChatClient, _NoRedirect, read_json, read_sse,
                         validate_capabilities)
from tool_recovery import inspect_recovery


def save(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def body(kind, stream=False, parsed=None):
    request = {'model': 'qwen3.8-flash-next', 'temperature': 0,
               'max_tokens': 256, 'stream': stream,
               'request_timeout_ms': 120000,
               'chat_template_kwargs': {'enable_thinking': kind == 'thinking',
                                        'reasoning_effort': 'low'}}
    if parsed is not None:
        request['parse_response'] = parsed
    if stream:
        request['stream_options'] = {'include_usage': True}
    if kind == 'tool':
        if parsed is True:
            request.update(tool_choice='auto', parallel_tool_calls=True)
        request['tools'] = [{'type': 'function', 'function': {
            'name': 'lookup', 'description': 'Look up an authoritative code.',
            'parameters': {'type': 'object', 'properties': {
                'key': {'type': 'string'}}, 'required': ['key']}}}]
        request['messages'] = [{'role': 'user', 'content':
            '请先调用lookup工具，key必须是audit。拿到工具结果后，'
            '只回复其中code字段的六位数字。在工具返回之前不要猜测答案。'}]
    elif kind == 'thinking':
        request['messages'] = [{'role': 'user', 'content':
            '计算17乘19。简短思考后，最终回答只写乘积的三位数字。'}]
    else:
        request['messages'] = [{'role': 'user', 'content': '只回复数字710003。'}]
    return request


def tool_history_case(reversed_results=False):
    request = body('tool', parsed=True)
    request['max_tokens'] = 64
    request['messages'] = [{'role': 'user', 'content':
        '读取下面已经完成的两个lookup结果。只回复key=alpha对应code的六位数字。'
        '不要再次调用工具，也不要回复beta对应的数字。'},
        {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': 'history-alpha', 'type': 'function', 'function': {
                'name': 'lookup', 'arguments': '{"key":"alpha"}'}},
            {'id': 'history-beta', 'type': 'function', 'function': {
                'name': 'lookup', 'arguments': '{"key":"beta"}'}}]}]
    results = [
        {'role': 'tool', 'tool_call_id': 'history-alpha',
         'content': '{"code":"831907"}'},
        {'role': 'tool', 'tool_call_id': 'history-beta',
         'content': '{"code":"442106"}'}]
    request['messages'].extend(reversed(results) if reversed_results else results)
    return request


def decode(raw, streaming):
    if not streaming:
        result = json.loads(raw)
        require('error' not in result, 'server returned an error')
        return dict(message=result['choices'][0]['message'],
                    finish=result['choices'][0]['finish_reason'],
                    usage=result['usage'])
    message = {'role': 'assistant', 'content': ''}
    tools, finish, usage = [], None, None
    done = 0
    for line in raw.splitlines():
        if not line:
            continue
        require(line.startswith('data: '), 'unexpected SSE line')
        data = line[6:]
        require(not done, 'event after DONE')
        if data == '[DONE]':
            done += 1
            continue
        event = json.loads(data)
        require('error' not in event, 'error SSE (not a successful finish)')
        if event.get('usage') is not None:
            require(usage is None and finish is not None, 'bad usage ordering')
            usage = event['usage']
        for choice in event['choices']:
            require(finish is None, 'choice after terminal')
            delta = choice['delta']
            if delta.get('role') is not None:
                require(delta['role'] == 'assistant', 'unexpected role')
            for key in ['content', 'reasoning_content']:
                if delta.get(key) is not None:
                    require(isinstance(delta[key], str), 'non-string delta')
                    message[key] = message.get(key, '') + delta[key]
            for call in delta.get('tool_calls', []):
                require(call['index'] == len(tools), 'non-contiguous tool index')
                require(call['type'] == 'function' and call['id'], 'tool identity')
                require(isinstance(call['function']['arguments'], str),
                        'arguments must be a JSON string')
                require(isinstance(json.loads(call['function']['arguments']), dict),
                        'arguments must decode to an object')
                tools.append({k: v for k, v in call.items() if k != 'index'})
            if choice.get('finish_reason') is not None:
                finish = choice['finish_reason']
    require(done == 1 and finish is not None and usage is not None,
            'incomplete SSE terminal/usage')
    if tools:
        message['tool_calls'] = tools
        if not message['content']:
            message['content'] = None
    return dict(message=message, finish=finish, usage=usage)


def comparable(result):
    result = copy.deepcopy(result)
    for call in result['message'].get('tool_calls', []):
        del call['id']  # Request-specific identity, checked separately on SSE.
    return result


class Run:
    def __init__(self, args):
        self.args = args
        self.out = args.output
        self.started = time.monotonic()
        self.requests = 0
        self.negative = 0
        self.starts = 0
        self.capability_gets = 0
        self.agent_runs = 0

    def budget(self):
        require(time.monotonic() - self.started < LIMITS['http_seconds'],
                '30-minute budget')
        require(self.requests <= LIMITS['generations'] and
                self.starts <= LIMITS['starts'] and
                self.negative <= LIMITS['negatives'] and
                self.capability_gets <= LIMITS['capability_gets'] and
                self.agent_runs <= LIMITS['agent_runs'],
                'frozen request/startup budget')

    def request(self, method, path, payload=None):
        if method == 'GET' and path == '/v1/capabilities':
            self.capability_gets += 1
        self.budget()
        remaining = LIMITS['http_seconds'] - (time.monotonic() - self.started)
        timeout = max(1, min(120, remaining))
        connection = http.client.HTTPConnection('127.0.0.1', self.args.port,
                                                timeout=timeout)
        try:
            connection.request(method, path,
                json.dumps(payload, ensure_ascii=False).encode('utf-8')
                if payload is not None else None,
                {'Content-Type': 'application/json'})
            response = connection.getresponse()
            return response.status, response.read().decode('utf-8')
        finally:
            connection.close()

    def chat(self, folder, name, request, negative=False):
        if negative:
            self.negative += 1
        else:
            self.requests += 1
        self.budget()
        request = copy.deepcopy(request)
        request['request_id'] = 'protocol-' + secrets.token_hex(16)
        request['cancel_token'] = secrets.token_hex(32)
        save(folder / (name + '-request.json'), request)
        start = time.monotonic()
        status, raw = self.request('POST', '/v1/chat/completions', request)
        save(folder / (name + '-response.json'), dict(status=status, raw=raw,
            elapsed_seconds=time.monotonic() - start))
        if negative:
            require(status == 400 and 'error' in json.loads(raw), name)
            return None
        require(status == 200, name + ': HTTP ' + str(status))
        parsed = decode(raw, request.get('stream', False))
        if request.get('parse_response') is True:
            # Reuse the same wire: exercise the integration client without an
            # extra generation, network retry, or model-selected tool dispatch.
            reader = read_sse if request.get('stream') else read_json
            client_result = reader(io.BytesIO(raw.encode('utf-8')))
            require(client_result.id == request['request_id'] and
                    client_result.model == request['model'],
                    'client response identity mismatch')
            require(dict(message=client_result.message,
                         finish=client_result.finish_reason,
                         usage=client_result.usage) == parsed,
                    'client/server protocol interpretation mismatch')
        save(folder / (name + '-parsed.json'), parsed)
        self.wait_slot(name)
        return parsed

    def wait_slot(self, name):
        released_by = time.monotonic() + 3
        while True:
            code, raw_health = self.request('GET', '/healthz')
            health = json.loads(raw_health)
            require(code == 200 and health['gpu_healthy'], name + ': unhealthy')
            if health['seq_slots_free'] == 1:
                break
            require(time.monotonic() < released_by, name + ': slot not released')
            time.sleep(0.01)

    def pair(self, folder, kind, check=None):
        plain = self.chat(folder, kind + '-parsed', body(kind, parsed=True))
        streamed = self.chat(folder, kind + '-stream', body(kind, True, True))
        require(comparable(plain) == comparable(streamed), kind + ': wire mismatch')
        if check:
            check(plain)
        return plain

    def tool(self, result):
        require(result['finish'] == 'tool_calls', 'missing tool_calls finish')
        calls = result['message'].get('tool_calls', [])
        require(len(calls) == 1 and calls[0]['function']['name'] == 'lookup',
                'model did not select the expected tool')
        require(json.loads(calls[0]['function']['arguments']) == {'key': 'audit'},
                'model generated incorrect arguments')

    def agent_roundtrip(self, folder, *, stream, mtp=False):
        self.agent_runs += 1
        self.budget()
        name = 'agent-sse' if stream else 'agent-json'
        journal = folder / name
        owner, trace, completions = self, [], []
        generation_count = 0

        class ObservedClient(ChatClient):
            def __init__(self, base_url):
                super().__init__(base_url)
                # This driver targets its own loopback server, never an env proxy.
                self.opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({}), _NoRedirect())

            def _open(self, request, timeout):
                nonlocal generation_count
                path = request.full_url.removeprefix(self.base)
                if request.method == 'POST' and path == '/chat/completions':
                    require(generation_count < LIMITS['agent_rounds'],
                            'demo generation limit')
                    generation_count += 1
                    owner.requests += 1
                elif request.method == 'GET' and path == '/capabilities':
                    owner.capability_gets += 1
                else:
                    raise AssertionError('unplanned demo transport')
                owner.budget()  # Reserve before any network side effect.
                remaining = (LIMITS['http_seconds'] -
                             (time.monotonic() - owner.started))
                require(remaining >= timeout, 'insufficient HTTP time budget')
                record = dict(method=request.method, path=path)
                trace.append(record)
                try:
                    response = super()._open(request, timeout)
                    record['status'] = response.status
                    record['content_type'] = response.headers.get_content_type()
                    return response
                except BaseException as error:
                    record['error'] = repr(error)
                    raise

            def capabilities(self, **kwargs):
                value = super().capabilities(**kwargs,
                    protocol_sha256=owner.args.protocol_sha256)
                require(value['runtime'] == dict(media_enabled=False,
                                                 mtp_enabled=mtp),
                        'demo runtime capability mismatch')
                return value

            def complete(self, request, *, sink=None):
                result = super().complete(request, sink=sink)
                owner.budget()
                owner.wait_slot(name)
                completions.append(dict(message=result.message,
                    finish=result.finish_reason, usage=result.usage))
                return result

        try:
            # Instrument only accounting; both requests use the real client's
            # HTTP transport and the unchanged demo dispatch/history loop.
            with patch.object(demo, 'ChatClient', ObservedClient):
                answer = demo.run(journal,
                    f'http://127.0.0.1:{self.args.port}', stream=stream,
                    max_rounds=LIMITS['agent_rounds'],
                    max_tool_calls=LIMITS['agent_tool_calls'],
                    max_tokens=LIMITS['agent_max_tokens'])
            require(answer.strip() == '831907' and generation_count == 2 and
                    len(completions) == 2, 'incomplete demo round trip')
            self.tool(completions[0])
            require(completions[1]['finish'] == 'stop', 'demo final finish')
            recovery = inspect_recovery(journal)
            save(folder / (name + '-recovery.json'), recovery)
            require(recovery['state'] == 'COMPLETED' and
                    recovery['requests'] == 2 and
                    recovery['completed_tool_calls'] == 1 and
                    recovery['automatic_retry'] is False,
                    'completed demo journal did not reconcile')
            save(folder / (name + '-parsed.json'), completions)
            return journal, completions
        finally:
            save(folder / (name + '-transport.json'), trace)

    def model(self, name, binary, mtp, action):
        self.starts += 1
        self.budget()
        folder = self.out / name
        folder.mkdir(mode=0o700)
        command = [str(binary), 'serve', '--model-dir', str(self.args.model_dir),
            '--host', '127.0.0.1', '--port', str(self.args.port), '--max-seq', '1',
            '--max-prefill', '2048', '--max-len', '8192']
        if not mtp:
            command.append('--no-mtp')
        save(folder / 'command.json', dict(argv=command, sha256=digest(binary)))
        env = {k: v for k, v in os.environ.items()
               if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
        server = None
        try:
            with (folder / 'server.log').open('x') as log:
                server = subprocess.Popen(command, cwd=ROOT, env=env,
                    stdout=log, stderr=subprocess.STDOUT)
                start = time.monotonic()
                while 'serving on port' not in (folder / 'server.log').read_text():
                    self.budget()
                    require(server.poll() is None, name + ': startup failed')
                    require(time.monotonic() - start < 240, 'startup timeout')
                    time.sleep(1)
                code, raw_caps = self.request('GET', '/v1/capabilities')
                save(folder / 'capabilities.json', dict(status=code, raw=raw_caps))
                if name.startswith('candidate-'):
                    require(code == 200, 'candidate capability endpoint missing')
                    caps = validate_capabilities(json.loads(raw_caps),
                        'qwen3.8-flash-next', protocol_sha256=self.args.protocol_sha256)
                    require(caps['runtime'] == dict(media_enabled=False,
                                                   mtp_enabled=mtp),
                            'capability runtime flags mismatch')
                else:
                    require(code == 404, 'frozen raw baseline unexpectedly changed')
                action(folder)
        finally:
            if server is not None and server.poll() is None:
                server.terminate()
                try:
                    server.wait(timeout=45)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=10)
            raw = (folder / 'server.log').read_text()
            clean = server is not None and server.returncode == 0 and (
                'shutdown complete (0 in-flight remaining)' in raw)
            save(folder / 'shutdown.json', dict(clean=clean,
                 returncode=server.returncode if server else None))
            require(clean, name + ': unclean shutdown')

    def run(self):
        reference = {}

        def baseline(folder):
            for kind in ['basic', 'tool']:
                reference[kind] = self.chat(folder, kind, body(kind))

        def plain(folder):
            for kind in ['basic', 'tool']:
                actual = self.chat(folder, kind + '-raw', body(kind, parsed=False))
                require(actual == reference[kind], kind + ': raw baseline changed')
            basic = self.pair(folder, 'basic')
            require(basic['message']['content'].strip() == '710003', 'basic quality')
            _, json_turns = self.agent_roundtrip(folder, stream=False)
            _, streamed = self.agent_roundtrip(folder, stream=True)
            require([comparable(r) for r in json_turns] ==
                    [comparable(r) for r in streamed], 'demo wire mismatch')
            ordered = self.chat(folder, 'history-ordered', tool_history_case())
            reordered = self.chat(folder, 'history-reordered', tool_history_case(True))
            require(comparable(ordered) == comparable(reordered),
                    'tool history order changed the canonical response')
            require(ordered['finish'] == 'stop' and
                    ordered['message']['content'].strip() == '831907',
                    'tool history association quality')
            raw = self.chat(folder, 'thinking-raw', body('thinking', parsed=False))
            thought = self.pair(folder, 'thinking')
            before, marker, after = raw['message']['content'].partition('</think>')
            require(marker and thought['message'].get('reasoning_content') == before
                    and thought['message']['content'] == after, 'thinking projection')
            require(after.strip() == '323', 'thinking answer quality')
            truncated = body('thinking', True, True)
            truncated['max_tokens'] = 1
            limited = self.chat(folder, 'length', truncated)
            require(limited['finish'] == 'length', 'length finish')
            for i, extra in enumerate([
                {'tool_choice': 'required'}, {'tool_choice': 'none'},
                {'tool_choice': {'type': 'function', 'function': {'name': 'lookup'}}},
                {'parallel_tool_calls': False}, {'parse_response': 'true'},
                {'parse_response': False, 'tool_choice': 'auto'},
                {'response_format': {'type': 'json_schema'}}, {'temperature': 1},
                {'tools': [{'type': 'function', 'function': {'name': 'x',
                                                           'strict': True}}]},
                {'tools': [{'type': 'custom', 'name': 'x'}]}]):
                request = body('tool')
                request.update(extra)
                self.chat(folder, 'negative-' + str(i), request, True)

        def mtp(folder):
            self.chat(folder, 'tool-raw', body('tool', parsed=False))
            journal, turns = self.agent_roundtrip(folder, stream=True, mtp=True)
            request = json.loads((journal / 'round-00-request.json').read_text())
            request['stream'] = False
            request.pop('stream_options')
            tool = self.chat(folder, 'tool-parsed', request)
            require(comparable(tool) == comparable(turns[0]),
                    'MTP tool wire mismatch')
            raw = self.chat(folder, 'thinking-raw', body('thinking', parsed=False))
            parsed = self.chat(folder, 'thinking-stream', body('thinking', True, True))
            before, marker, after = raw['message']['content'].partition('</think>')
            require(marker and parsed['message'].get('reasoning_content') == before
                    and parsed['message']['content'] == after and after.strip() == '323',
                    'MTP reasoning output')
            truncated = body('thinking', True, True)
            truncated['max_tokens'] = 1
            require(self.chat(folder, 'length', truncated)['finish'] == 'length',
                    'MTP length finish')

        self.model('baseline-plain', self.args.baseline, False, baseline)
        self.model('candidate-plain', self.args.candidate, False, plain)
        self.model('candidate-mtp', self.args.candidate, True, mtp)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--model-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--port', type=int, default=18096)
    parser.add_argument('--protocol-sha256', required=True)
    args = parser.parse_args()
    for key in ['candidate', 'baseline', 'model_dir']:
        setattr(args, key, getattr(args, key).resolve(strict=True))
    args.output = args.output.resolve()
    require(args.output.is_relative_to(ROOT / '.q4t-work'), 'output location')
    # Convention: --output STAGE/http with the outer guard writing STAGE/lease.
    require_guard(args.output.parent)
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    run = Run(args)
    status, error = 'FAIL', None
    try:
        run.run()
        run.budget()
        require((run.requests, run.negative, run.starts,
                 run.capability_gets, run.agent_runs) == tuple(LIMITS[k] for k in
                ('planned_generations', 'negatives', 'starts',
                 'capability_gets', 'agent_runs')),
                'planned HTTP coverage incomplete')
        status = 'PASS'
    except BaseException as exception:
        error = repr(exception)
        raise
    finally:
        save(args.output / 'result.json', dict(status=status, error=error,
             requests=run.requests, negative=run.negative, starts=run.starts,
             capability_gets=run.capability_gets, agent_runs=run.agent_runs,
             elapsed_seconds=time.monotonic() - run.started,
             limitation='Bounded functional checks; no performance acceptance.'))


if __name__ == '__main__':
    main()

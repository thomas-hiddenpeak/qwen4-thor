"""Native web business: two starts/eight requests/900s, one GPU lease."""
import argparse
from dataclasses import asdict
import http.client
import io
import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.examples.chat_client import read_json, read_sse
from tools.serve_responses import acceptance as foreground
from tools.serve_responses.background_acceptance import probe_ports
from tools.serve_responses.test_contract import errors
from tools.serve_service.package import read, require, save, sha

base = foreground.base
LIMITS = dict(starts=2, chats=8, controls=2000, seconds=900)
URL = 'https://example.com/'
MARKER = '[source](<' + URL + '>)'


def policy(networks=()):
    return dict(version=1, seeds=[URL], scopes=['local:anonymous'],
        max_pages=1, max_depth=0, max_download_bytes=1048576,
        refresh_seconds=3600, retention_seconds=86400,
        fetch=dict(public_networks=True, allow_http=False, ports=[443],
                   allowed_hosts=['example.com'], allowed_networks=list(networks),
                   timeout_ms=15000))


def requests():
    question = ('According to Example Domain, what is this domain intended '
        'for? Answer in one short sentence and copy the exact citation_marker '
        'of the supporting retrieved source. Use the word documentation.')
    chat = dict(model=foreground.MODEL, temperature=0, enable_thinking=False,
        max_completion_tokens=192, web_search_options=dict(search_context_size='low'),
        messages=[dict(role='user', content=question)])
    response = dict(model=foreground.MODEL, temperature=0,
        reasoning=dict(effort='none'), max_output_tokens=512,
        input=question, max_tool_calls=1, parallel_tool_calls=False,
        tools=[dict(type='web_search', search_context_size='low')],
        tool_choice=dict(type='web_search'),
        instructions='Search once with one short query about Example Domain '
        'documentation. Then answer only from the actual returned source. '
        'Copy its citation_marker exactly, including angle brackets. '
        'Do not invent a source or make another tool call.')
    return dict(chat_json=chat,
        chat_sse=dict(chat, stream=True, stream_options=dict(include_usage=True)),
        included=dict(response, include=['web_search_call.action.sources']),
        offline=dict(response, tools=[dict(type='web_search',
            external_web_access=False, search_context_size='low')], stream=True,
            stream_options=dict(include_obfuscation=False)),
        none=dict(response, input='Compute 17 + 25. Reply only with the decimal number.',
            instructions='', tool_choice='none', max_output_tokens=64),
        history=dict(model=foreground.MODEL, temperature=0,
            reasoning=dict(effort='none'), max_output_tokens=192,
            input='From the source you already retrieved, repeat what Example '
            'Domain is for in one sentence, including documentation and the '
            'same exact source citation_marker. Do not search again.'),
        missing=chat,
        healthy=dict(model=foreground.MODEL, temperature=0, enable_thinking=False,
            max_completion_tokens=32, messages=[dict(role='user',
                content='Compute 17 + 25. Reply only with the decimal number.')]))


def check_citations(text, annotations, *, chat=False):
    require('documentation' in text.lower() and MARKER in text,
            'answer did not use the retrieved source and exact citation')
    require(annotations, 'actual source annotation missing')
    for item in annotations:
        require(item['type'] == 'url_citation', 'unexpected citation kind')
        citation = item['url_citation'] if chat else item
        require(citation['url'] == URL and citation['title'] == 'Example Domain',
                'source identity differs')
        require(text[citation['start_index']:citation['end_index']] == MARKER,
                'actual Unicode source offsets differ')


def check_response(value, *, included=False, history=False):
    problems = errors(value, 'response')
    require(not problems, 'Responses schema: ' + str([
        (list(e.path), e.message[:100]) for e in problems]))
    require(value['status'] == 'completed' and value['error'] is None,
            'native web answer did not complete')
    foreground.check_usage(value)
    calls = [item for item in value['output'] if item['type'] == 'web_search_call']
    require(len(calls) == (0 if history else 1), 'wrong number of web calls')
    for call in calls:
        require(call['status'] == 'completed' and call['action']['type'] == 'search',
                'web search did not complete')
        if included:
            require(call['action']['sources'] == [dict(type='url', url=URL)],
                    'actual source list differs')
        else:
            require('sources' not in call['action'], 'source include leak')
    parts = [part for item in value['output'] if item['type'] == 'message'
             for part in item['content'] if part['type'] == 'output_text']
    require(len(parts) == 1, 'missing final answer')
    check_citations(parts[0]['text'], parts[0]['annotations'])


def prepare(args):
    base.prepare(args, rows=requests(), limits=LIMITS, scope=__doc__)
    out = args.output.resolve()
    plan = read(out/'plan.json')
    names = subprocess.check_output(['git', 'ls-files', 'tools/serve_responses',
        'docs/serve-application-v1.schema.json', 'docs/serve-config-v1.schema.json'],
        cwd=ROOT, text=True).splitlines()
    plan['sources'].update({name:sha(ROOT/name) for name in names})
    reference = args.source.resolve(strict=True)
    require(reference.is_relative_to(ROOT/'.q4t-work') and
            0 < reference.stat().st_size <= 1048576, 'public source receipt')
    require(b'documentation examples' in reference.read_bytes(), 'unexpected source')
    shutil.copyfile(reference, out/'public-source.html')
    plan['source_reference'] = dict(url=URL, path=str(out/'public-source.html'),
                                   sha256=sha(reference))
    networks = args.source_network or []
    for text in networks:
        network = ipaddress.ip_network(text, strict=True)
        require(network.prefixlen == network.max_prefixlen,
                'preflight may grant only an explicit single source address')
    save(out/'web-policy.json', policy(networks))
    (out/'web-policy.json').chmod(0o600)
    checked = subprocess.run([plan['binary'], 'web-policy-check',
        str(out/'web-policy.json')], capture_output=True, timeout=10)
    (out/'policy-check.stdout').write_bytes(checked.stdout)
    (out/'policy-check.stderr').write_bytes(checked.stderr)
    require(checked.returncode == 0 and not checked.stderr, 'native policy precheck')
    plan['web_policy'] = dict(path=str(out/'web-policy.json'),
        sha256=sha(out/'web-policy.json'), description=json.loads(checked.stdout))
    plan['derived_requests'] = [
        'one Chat preflight proves actual source digest/text before generation',
        'history previous_response_id comes from the included real search response',
        'retrieve/replay use actual response ID and are control requests',
        'second start omits optional search policy; no state/config mutation',
        'no HTTP or model automatic retries; only owned processes stopped']
    save(out/'plan.json', plan)


class Stage(foreground.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_NATIVE_WEB_SEARCH_BUSINESS'

    def start(self):
        probe_ports((self.plan['port'], self.plan['operations_port']))
        for binding in ('web_policy', 'source_reference'):
            require(sha(self.plan[binding]['path']) == self.plan[binding]['sha256'],
                    'frozen web input drift: ' + binding)
        generation = self.counts['starts'] + 1
        args = [self.plan['binary'], 'serve', '--model-dir', self.plan['model'],
            '--host', '127.0.0.1', '--port', str(self.plan['port']),
            '--operations-port', str(self.plan['operations_port']), '--mtp',
            '--max-seq', '1', '--max-len', '8192', '--max-prefill', '2048',
            '--max-tokens', '512', '--max-inflight-requests', '8',
            '--max-queued-requests', '2', '--object-store-dir', str(self.out/'objects')]
        if generation == 1:
            args += ['--web-policy-file', self.plan['web_policy']['path']]
        save(self.out/f'model-{generation}-command.json', args)
        self.budget('starts')
        with (self.out/f'model-{generation}.log').open('xb', buffering=0) as log:
            self.model = subprocess.Popen(args, cwd=ROOT, stdout=log,
                stderr=subprocess.STDOUT, pass_fds=(self.lease,))
        self.model_identity = base.lifecycle.guard.processes()[self.model.pid]
        self.history[self.model.pid] = self.model_identity
        save(self.out/f'model-{generation}-identity.json',
             dict(pid=self.model.pid, **self.model_identity))
        end = time.monotonic()+180
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
            status, raw = self.http('/v1/'+name, label=f'initial-{generation}-'+name)
            require(status == 200, name+' unavailable')
            save(self.out/f'{name}-{generation}.json', json.loads(raw))
        config = read(self.out/f'config-{generation}.json')
        require(config['web_deployment']['configured'] == (generation == 1) and
                not config['web_deployment']['core_readiness_dependency'],
                'optional web configuration differs')
        self.client.protocol_sha256 = read(
            self.out/f'capabilities-{generation}.json')['protocol_source_sha256']

    def chat(self, label, body):
        self.budget('chats')
        save(self.out/(label+'.request.json'), body)
        with (self.out/(label+'.wire')).open('xb') as wire:
            result = self.client.complete(body, sink=wire.write)
        save(self.out/(label+'.sdk.json'), self.client.last_result)
        save(self.out/(label+'.result.json'), asdict(result))
        raw = (self.out/(label+'.wire')).read_bytes()
        own = (read_sse if body.get('stream') else read_json)(io.BytesIO(raw))
        require(own.message == result.message, 'own and official Chat clients differ')
        self.same_model()
        self.drained(label+'-drain')
        require(result.finish_reason == 'stop', 'Chat did not complete')
        return result

    def business(self):
        rows = self.plan['requests']
        self.budget('controls')
        status, raw = self.http('/v1/chat/preflight', dict(
            request=rows['chat_json'], include_prompt=True), label='source-preflight')
        require(status == 200, 'source preflight failed')
        prompt = json.loads(raw)['prompt']
        require(self.plan['source_reference']['sha256'] in prompt and
                'documentation examples' in prompt and URL in prompt,
                'native retrieval does not match independently observed HTTPS source')
        for label in ('chat_json', 'chat_sse'):
            answer = self.chat(label, rows[label])
            check_citations(answer.message['content'],
                            answer.message.get('annotations', []), chat=True)
        included = self.sdk('included', 'create', rows['included'])
        check_response(included, included=True)
        projected = self.sdk('stored-hidden', 'retrieve', response_id=included['id'])
        check_response(projected)
        offline = self.sdk('offline', 'create', rows['offline'])
        check_response(offline)
        none = self.sdk('none', 'create', rows['none'])
        require(none['status'] == 'completed' and foreground.text(none).strip() == '42'
                and not any(item['type'] == 'web_search_call' for item in none['output']),
                'tool_choice none did not preserve plain LLM generation')
        history = self.sdk('history', 'create', dict(rows['history'],
                           previous_response_id=included['id']))
        check_response(history, history=True)
        self.cleanup()
        (self.out/'cleanup.json').rename(self.out/'cleanup-first.json')
        self.model = self.model_identity = None
        self.start()
        status, raw = self.submit('missing', rows['missing'])
        require(status == 503 and json.loads(raw)['error']['code'] ==
                'web_search_not_configured', 'missing backend did not fail locally')
        self.same_model()
        require(self.http('/readyz')[0] == 200, 'web failure changed core readiness')
        answer = self.chat('healthy', rows['healthy'])
        require(answer.message['content'].strip() == '42',
                'healthy core failed after search dependency unavailable')
        require(self.counts['starts'] == 2 and self.counts['chats'] == 8,
                'business request count drift')
        for path, digest in self.plan['protected'].items():
            require(sha(path) == digest, 'published default changed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    p = sub.add_parser('prepare')
    p.add_argument('--binary', type=Path, required=True)
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--source-network', action='append')
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--port', type=int, default=18164)
    p = sub.add_parser('run')
    p.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'prepare':
        prepare(args)
    else:
        with base.lifecycle.child_subreaper():
            Stage(read(args.plan)).run()


if __name__ == '__main__':
    main()

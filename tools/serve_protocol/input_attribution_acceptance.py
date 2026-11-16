"""One model, six generations: attribution, actual tiers and Responses defaults."""
import argparse
import copy
import hashlib
import http.client
import json
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_protocol import chat_style_acceptance as style
from tools.serve_responses import acceptance as responses
from tools.serve_responses.sdk_capture import wire_events
from tools.serve_responses.test_contract import errors
from tools.serve_service.package import read, require, save, sha

base = style.base
LIMITS = dict(starts=1, chats=6, controls=2000, seconds=900)
DOCUMENT = style.DOCUMENT
IDENTIFIER = 'attribution-check-资料-reader'


def requests():
    old = style.requests()
    rows = dict(
        json=dict(old['default'], store=True, service_tier='fast',
                  safety_identifier=IDENTIFIER),
        sse=dict(old['default'], reasoning_effort='none', stream=True,
                 stream_options=dict(include_usage=True),
                 service_tier=None, safety_identifier=None),
        tool=dict(old['tool'], service_tier='priority', safety_identifier=IDENTIFIER),
        follow=copy.deepcopy(old['follow']))
    rows['follow'].update(max_completion_tokens=384, service_tier='flex',
                          safety_identifier=IDENTIFIER)
    rows['follow']['response_format']['json_schema']['schema'] = base.closed(dict(
        title=dict(type='string'), citation=dict(type='string'),
        sha256=dict(type='string', pattern='^[0-9a-f]{64}$')))
    rows['follow']['messages'] = [dict(role='user', content=
        'Return the actual document title, exact citation and source sha256 from '
        'the read result as JSON. Do not call another tool.')]
    common = dict(model=responses.MODEL, input=style.QUESTION, temperature=0,
                  max_output_tokens=384, store=True, safety_identifier=IDENTIFIER)
    rows.update(response_default=dict(common, service_tier='fast'),
                response_null=dict(common, reasoning=None, service_tier=None,
                                   stream=True, stream_options=dict(include_obfuscation=False)))
    for label, body in rows.items():
        body['request_id'] = 'input-attribution-' + label
        body['cancel_token'] = hashlib.sha256(label.encode()).hexdigest()
    return rows


def expected_digest(identifier, scope='local:anonymous'):
    bound = f'q4t-safety-v1:{len(scope.encode())}:{scope}:{identifier}'
    return hashlib.sha256(bound.encode()).hexdigest()


def chat_wire(raw, stream):
    values = [json.loads(line[6:]) for line in raw.splitlines()
              if line.startswith(b'data: {')] if stream else [json.loads(raw)]
    require(values and all(v.get('service_tier') == 'default' for v in values),
            'Chat must report actual default tier, including final usage')
    require(all('safety_identifier' not in v and 'request_attribution' not in v
                for v in values), 'private attribution entered public Chat')
    if stream:
        require(values[-1]['choices'] == [] and values[-1].get('usage'),
                'missing final usage')
    return dict(events=len(values), actual_tier='default')


def check_response(value):
    require(not errors(value, 'response'), 'Responses schema failed')
    require(value['status'] == 'completed' and value['service_tier'] == 'default' and
            value['reasoning']['effort'] == 'medium', 'Responses effective options differ')
    require(responses.text(value).strip() == '42', 'Responses answer differs')
    responses.check_usage(value)


def prepare(args):
    base.prepare(args, rows=requests(), limits=LIMITS, scope=__doc__)
    path = args.output.resolve() / 'plan.json'
    plan = read(path)
    for name in subprocess.check_output(
            ['git', 'ls-files', 'tools/serve_responses'], cwd=ROOT, text=True).splitlines():
        plan['sources'][name] = sha(ROOT / name)
    plan['planned'] = dict(starts=1, chats=6, model_steps=6, actual_document_reads=1)
    plan['mutations'] = ['one own model and private object store; graceful own cleanup']
    plan['derived_requests'] = [
        'follow history contains actual successful tool call and exactly one real document read',
        'title/citation/source sha256 are validated against that read, never supplied as expected answers',
        'stored GET/DELETE use only this run actual IDs; no generation retry',
        'new attribution never changes prompts, ownership, quota or scheduler pool']
    save(path, plan)


class Stage(responses.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_INPUT_ATTRIBUTION_BUSINESS'

    def chat(self, label, body):
        value = self.sdk_request(label, body)
        raw = (self.out / (label + '.wire')).read_bytes()
        chat_wire(raw, body.get('stream', False))
        if body.get('stream'):
            style.check_padding(raw, body['stream_options'].get('include_obfuscation', True))
        preflight = json.loads((self.out / (label + '-preflight.wire')).read_bytes())
        prompt = preflight['prompt']
        require(IDENTIFIER not in prompt, 'attribution entered model prompt')
        return value

    def control(self, method, route, label):
        self.budget('controls')
        connection = http.client.HTTPConnection('127.0.0.1', self.plan['port'], timeout=30)
        try:
            connection.request(method, route)
            reply = connection.getresponse()
            raw = reply.read(4 * 1024 * 1024 + 1)
            require(len(raw) <= 4 * 1024 * 1024, 'control body limit')
            (self.out / (label + '.wire')).write_bytes(raw)
            save(self.out / (label + '.http.json'), dict(method=method, route=route, status=reply.status))
            require(reply.status == 200, 'owned stored Chat control failed')
            self.same_model()
            return json.loads(raw)
        finally:
            connection.close()

    def business(self):
        rows = copy.deepcopy(self.plan['requests'])
        first = self.chat('json', rows['json'])
        style.check_answer(first)
        stored = self.control('GET', '/v1/chat/completions/' + first.id, 'chat-get')
        chat_wire(json.dumps(stored).encode(), False)
        require(stored['choices'][0]['message'] == first.message, 'stored answer changed')
        style.check_answer(self.chat('sse', rows['sse']))
        call = self.chat('tool', rows['tool'])
        calls = base.prepare_tool_calls(call, base.VALIDATORS)
        require(len(calls) == 1 and calls[0].arguments == dict(path=DOCUMENT,
                start_line=1, line_count=16), 'tool identity differs')
        result = base.ProjectRecords(ROOT).execute(calls[0])
        require(result['ok'] and result['source']['sha256'] == self.plan['sources'][DOCUMENT],
                'actual project source differs')
        save(self.out / 'tool-execution.json', dict(executions=1, result=result))
        follow = rows['follow']
        instructions = follow['messages']
        follow['messages'] = base.append_tool_results(rows['tool']['messages'], call,
            {calls[0].call_id: json.dumps(result, ensure_ascii=False)}) + instructions
        final = self.chat('follow', follow)
        require(final.finish_reason == 'stop' and not final.message.get('tool_calls'),
                'tool continuation terminal differs')
        actual = json.loads(final.message['content'])
        title = result['source']['text'].splitlines()[0].split(': ', 1)[1]
        require(actual['title'] in (title, title.lstrip('# ')) and
                actual['citation'] == result['source']['citation'] and
                actual['sha256'] == result['source']['sha256'], 'document closure differs')
        response_ids = []
        for label in ('response_default', 'response_null'):
            value = self.sdk(label, 'create', rows[label])
            check_response(value)
            if rows[label].get('stream'):
                events = wire_events((self.out / (label + '.wire')).read_text())
                require(all(not errors(e, 'response_event') for e in events), 'SSE schema')
                require(all(e['response']['reasoning']['effort'] == 'medium' and
                    e['response']['service_tier'] == 'default' for e in events
                    if 'response' in e), 'Responses options changed during stream')
            saved = self.sdk(label + '-get', 'retrieve', response_id=value['id'],
                             query=dict(include=['message.output_text.logprobs']))
            require(saved == value, 'stored Responses result changed')
            response_ids.append(value['id'])
        metrics = self.drained('final')
        require(metrics['q4t_requests_total'] == 6 and metrics['q4t_requests_success_total'] == 6 and
                metrics['q4t_requests_error_total'] == 0, 'request replay/error')
        log = (self.out / 'model.log').read_text()
        require(IDENTIFIER not in log, 'raw attribution leaked into diagnostics')
        for label, body in rows.items():
            lines = [line for line in log.splitlines() if '[q4t][request]' in line and
                     'event=end' in line and 'id=' + body['request_id'] + ' ' in line]
            expected = expected_digest(IDENTIFIER) if body.get('safety_identifier') else '-'
            require(len(lines) == 1 and 'safety_identifier_sha256=' + expected in lines[0] and
                    'outcome=success' in lines[0], 'request attribution differs: ' + label)
        self.control('DELETE', '/v1/chat/completions/' + first.id, 'chat-delete')
        for index, identifier in enumerate(response_ids):
            self.sdk('response-delete-' + str(index), 'delete', response_id=identifier)
        save(self.out / 'business-summary.json', dict(starts=1, generations=6,
            actual_document=actual, actual_document_reads=1, actual_tier='default',
            new_responses_effort='medium', private_attribution_sha256=expected_digest(IDENTIFIER),
            same_model_pid=True, all_owned_objects_deleted=True, performance_claim=False))

    def cleanup(self):
        super().cleanup()
        for name, digest in self.plan['protected'].items():
            require(sha(name) == digest, 'default changed during short acceptance')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    command = modes.add_parser('prepare')
    command.add_argument('--binary', type=Path, required=True)
    command.add_argument('--output', type=Path, required=True)
    command.add_argument('--port', type=int, default=18188)
    command = modes.add_parser('run')
    command.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'prepare':
        prepare(args)
    else:
        with base.lifecycle.child_subreaper():
            Stage(read(args.plan)).run()


if __name__ == '__main__':
    main()

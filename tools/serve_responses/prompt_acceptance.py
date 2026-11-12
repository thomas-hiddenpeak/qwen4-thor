"""Actual versioned native Prompt context and tools: 2 starts/8 requests/900s."""
import argparse
import copy
import http.client
import json
import re
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_responses import acceptance as foreground
from tools.serve_responses import background_acceptance as background
from tools.serve_responses import encryption_acceptance as encrypted
from tools.serve_responses import summary_acceptance as summary
from tools.serve_responses.test_contract import errors
from tools.serve_service.package import read, require, save

LIMITS = dict(starts=2, chats=8, controls=2000, seconds=900)
MAX_MODEL_STEPS = 7


def definitions():
    common = dict(model=foreground.MODEL, temperature=0,
                  reasoning=dict(effort='none'), max_output_tokens=64)
    def arithmetic(value):
        return dict(request=dict(common,
            instructions='Answer the current question from the authoritative '
                         'record. Reply only with the decimal project code.',
            input=[dict(role='user', content='The authoritative project code is '+
                        str(value)+'. {{context}}')]),
            variables=dict(context='input_text'))
    tool = foreground.requests()['tool']
    template = {k: copy.deepcopy(v) for k, v in tool.items()
                if k not in ('stream', 'stream_options')}
    return dict(first=arithmetic(42), second=arithmetic(73),
                tool=dict(request=template, variables={}))


def requests():
    reference = dict(id='prompt_placeholder', variables=dict(context=dict(
        type='input_text', text='Keep this record available for the question.')))
    common = dict(prompt=reference, input='What is the authoritative project code?', store=True)
    first = copy.deepcopy(common)
    second = copy.deepcopy(common)
    second.update(stream=True)  # Actual default delta obfuscation.
    explicit = copy.deepcopy(second)
    explicit['prompt']['version'] = '1'
    tool = dict(prompt=dict(id='prompt_tool_placeholder'), store=True,
        background=True, stream=True, stream_options=dict(include_obfuscation=False))
    bad = copy.deepcopy(common)
    bad['prompt']['variables']['context'] = 42
    return dict(first=first, second=second, explicit=explicit, tool=tool,
        restarted=copy.deepcopy(common), follow=foreground.requests()['follow'],
        invalid=bad, chat=foreground.requests()['chat'])


def check_value(value, prompt_id, version, expected):
    background.check_response(value, 'completed')
    require(foreground.text(value).strip() == str(expected),
            'model did not use the selected template context')
    require(value['prompt']['id'] == prompt_id and
            value['prompt']['version'] == version, 'resolved prompt version drift')
    require(value['usage']['input_tokens'] > 0 and value['usage']['output_tokens'] > 0,
            'actual template usage absent')


def check_delta_padding(result):
    events = result.get('chunks') or []
    require(events and any(e['type'].endswith('.delta') for e in events), 'no real deltas')
    for event in events:
        require(('obfuscation' in event) == event['type'].endswith('.delta'),
                'SSE padding leaked to lifecycle event or missing on delta')
        require(not errors(event, 'response_event'), 'actual event outside pinned contract')


def prepare(args):
    background.prepare(args, rows=requests(), limits=LIMITS, scope=__doc__)
    path = args.output.resolve()/'plan.json'
    plan = read(path)
    plan['prompt_definitions'] = definitions()
    plan['planned'] = dict(starts=2, chats=8, internal_model_steps=MAX_MODEL_STEPS,
                           negative_before_execution=1)
    plan['mutations'] = [
        'only owned model and isolated synthetic client/object state',
        'create/append/select/delete only prompts created by this driver',
        'graceful owned stop and one explicit restart in the same strict lease']
    plan['derived_requests'] = [
        'native create/append/select/get/list/delete with actual IDs and CAS revisions; no retries',
        'SDK Responses prompt ID is replaced only with its actually created local ID',
        'v1 record42/v2 record73 appear only in registered templates; caller asks same question',
        'explicit old version and post-restart default version execute actual model',
        'background template emits document tool; actual docs/MODEL.md lines1-3 execute once',
        'delete tool template before restart, retrieve/replay saved completed response, actual tool output follows',
        'reprojected SSE uses delta-only padding, original saved event sequence is preserved',
        'invalid typed variable rejects before sampling; same-PID ordinary MTP Chat follows']
    save(path, plan)


class Stage(encrypted.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_RESPONSES_PROMPT_BUSINESS'

    def admin(self, label, method, path, body=None, status=200):
        self.budget('controls')
        self.same_model()
        headers = dict(encrypted.HEADERS)
        if body is not None:
            headers['Content-Type'] = 'application/json'
        conn = http.client.HTTPConnection('127.0.0.1', self.plan['port'], timeout=10)
        start = time.monotonic()
        try:
            conn.request(method, path, headers=headers,
                body=None if body is None else json.dumps(body, ensure_ascii=False).encode())
            response = conn.getresponse()
            raw = response.read(4*1024*1024+1)
            require(len(raw) <= 4*1024*1024, 'registry response bound')
            (self.out/(label+'.wire')).write_bytes(raw)
            save(self.out/(label+'.http.json'), dict(method=method, path=path,
                request=body, status=response.status, seconds=time.monotonic()-start))
            require(response.status == status, 'registry status: '+label+' '+raw[:256].decode(errors='replace'))
            value = json.loads(raw)
            if status == 200:
                name = {'prompt':'prompt_resource', 'prompt.version':'prompt_version',
                        'prompt.deleted':'prompt_deleted', 'list':'prompt_list'}[value['object']]
                require(not errors(value, name), 'registry schema '+label)
            self.same_model()
            return value
        finally:
            conn.close()

    def business(self):
        require(self.plan['prompt_definitions'] == definitions(), 'template drift')
        rows = copy.deepcopy(self.plan['requests'])
        native = self.admin('register-first', 'POST', '/v1/prompts',
                             dict(definition=definitions()['first']))
        identity = native['id']; path = '/v1/prompts/'+identity
        require(native['revision'] == 1, 'initial revision')
        for name in ('first', 'second', 'explicit', 'restarted', 'invalid'):
            rows[name]['prompt']['id'] = identity
        first = self.sdk('first', 'create', rows['first'])
        check_value(first, identity, '1', 42)
        history = self.sdk('first-inputs', 'input_items', response_id=first['id'],
                           query=dict(order='asc'))
        serialized = json.dumps(history, ensure_ascii=False)
        require('authoritative project code is 42' in serialized and '{{context}}' not in serialized,
                'effective template did not enter actual persisted input')
        first_version = self.admin('version-one', 'GET', path+'/versions/1')
        updated = self.admin('append-second', 'POST', path+'/versions',
            dict(definition=definitions()['second'], expected_revision=1))
        require(updated['default_version'] == '2' and updated['revision'] == 2, 'version append')
        self.admin('stale-append', 'POST', path+'/versions',
            dict(definition=definitions()['first'], expected_revision=1), status=409)
        require(self.admin('version-one-retained', 'GET', path+'/versions/1') == first_version,
                'immutable first version changed')
        second = self.sdk('second', 'create', rows['second'])
        check_value(second, identity, '2', 73)
        check_delta_padding(read(self.out/'second.sdk.json'))
        explicit = self.sdk('explicit-first', 'create', rows['explicit'])
        check_value(explicit, identity, '1', 42)
        # Alias rollback and restore affect future requests only.
        selected = self.admin('select-first', 'POST', path,
                              dict(default_version='1', expected_revision=2))
        require(selected['revision'] == 3, 'selection revision')
        selected = self.admin('select-second', 'POST', path,
                              dict(default_version='2', expected_revision=3))
        require(selected['revision'] == 4, 'selection restoration')
        tool_prompt = self.admin('register-tool', 'POST', '/v1/prompts',
                                 dict(definition=definitions()['tool']))
        rows['tool']['prompt']['id'] = tool_prompt['id']
        tool = self.sdk('background-tool', 'create', rows['tool'])
        require(tool['prompt']['version'] == '1', 'tool version not pinned')
        calls = [item for item in tool['output'] if item['type'] == 'function_call']
        require(len(calls) == 1 and calls[0]['status'] == 'completed' and
                calls[0]['name'] == 'read_project_record', 'actual document call absent')
        arguments = json.loads(calls[0]['arguments'])
        require(arguments == dict(path=foreground.DOCUMENT, start_line=1, line_count=3),
                'actual document arguments')
        result = foreground.base.ProjectRecords(ROOT).read(arguments)
        require(result['ok'] and result['source']['sha256'] == self.plan['sources'][foreground.DOCUMENT],
                'document source identity')
        save(self.out/'actual-tool.json', dict(call=calls[0], result=result))
        self.admin('delete-tool-template', 'DELETE', '/v1/prompts/'+tool_prompt['id'])
        self.accounting('before-restart', [first, second, explicit, tool])
        self.cleanup()
        cleanup = read(self.out/'cleanup.json');save(self.out/'first-cleanup.json',cleanup)
        require(cleanup['model_exit'] == 0, 'owned first model did not stop gracefully')
        self.model = None
        self.start()
        require(self.admin('version-after-restart', 'GET', path+'/versions/1') == first_version,
                'registry changed after restart')
        restarted = self.sdk('restarted-default', 'create', rows['restarted'])
        check_value(restarted, identity, '2', 73)
        replay = self.sdk('background-replay', 'retrieve', response_id=tool['id'],
                          query=dict(stream=True, include_obfuscation=False))
        require(replay == tool and read(self.out/'background-replay.sdk.json')['chunks'] ==
                read(self.out/'background-tool.sdk.json')['chunks'], 'saved events changed after template deletion/restart')
        projected = self.sdk('delta-projection', 'retrieve', response_id=second['id'],
                             query=dict(stream=True))
        check_value(projected, identity, '2', 73)
        check_delta_padding(read(self.out/'delta-projection.sdk.json'))
        follow = rows['follow']
        follow['previous_response_id'] = tool['id']
        follow['input'] = [dict(type='function_call_output', call_id=calls[0]['call_id'],
                               output=json.dumps(result, ensure_ascii=False))]
        final = self.sdk('document-follow', 'create', follow)
        actual = json.loads(foreground.text(final))
        title = result['source']['text'].splitlines()[0].split(': ', 1)[1]
        require(actual['title'] in (title, title.lstrip('# ')) and
                actual['citation'] == result['source']['citation'], 'document closure')
        self.sdk('invalid-variable', 'create', rows['invalid'], expected_status=400)
        self.budget('chats')
        status, raw = self.http('/v1/chat/completions', rows['chat'], label='same-pid-chat')
        chat = json.loads(raw)
        require(status == 200 and chat['choices'][0]['finish_reason'] == 'stop' and
                chat['choices'][0]['message']['content'].strip() == '42', 'ordinary Chat after rejected prompt')
        self.accounting('after-restart', [restarted, final], chat=chat, rejected=1)
        logs = ''.join((self.out/f'model-{i}.log').read_text() for i in (1, 2))
        for value in (first, second, explicit, tool, restarted, final):
            summary.check_steps(logs, value, 1)
        require(len(re.findall(r'^\[sampling\] request=', logs, re.M)) == MAX_MODEL_STEPS,
                'unexpected model step/replay')
        require('[mtp-accept] request='+chat['id'] in logs, 'ordinary MTP absent')
        save(self.out/'business-checks.json', dict(external_attempts=8,
            internal_model_steps=MAX_MODEL_STEPS, actual_tool_executions=1,
            actual_version_results={'1':42,'2':73}, original_background_replay=True,
            deleted_template_does_not_change_stored_response=True, exact_client_usage=True,
            delta_only_live_and_projected_padding=True, invalid_variable_before_model=True,
            direct_engine_rpm_persistence_claim=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    command = modes.add_parser('prepare')
    command.add_argument('--binary', type=Path, required=True)
    command.add_argument('--output', type=Path, required=True)
    command.add_argument('--port', type=int, default=18989)
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

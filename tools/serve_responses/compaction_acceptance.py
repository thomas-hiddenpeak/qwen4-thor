"""Actual compact memory/continuation and tools, 2 starts/8 requests/900s."""
import argparse
import base64
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys

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
MAX_MODEL_STEPS = 9
# Reuse the already verified isolated synthetic client transport. This ID and
# key exist only in this driver's private test policy, never production policy.
HEADERS = encrypted.HEADERS


def history(last='What was the verified result? Reply only with the decimal number.'):
    # Deliberately redundant data makes a real reduction possible while keeping
    # the unique fact in old history, not in the new question or hidden oracle.
    return [dict(role='user', content='Remember the verified result of 17 + 25.'),
        dict(role='assistant', content='The verified result is 42. ' +
            'This is a redundant record of the same completed calculation. ' * 100),
        dict(role='user', content=last)]


def requests():
    old = foreground.requests()
    common = dict(model=foreground.MODEL, temperature=0, store=False,
                  reasoning=dict(effort='none'), max_output_tokens=192)
    tool = copy.deepcopy(old['tool'])
    tool.update(store=True, background=True, max_output_tokens=512,
                input=history(old['tool']['input']) + [dict(type='compaction_trigger')])
    auto = dict(common, input=history(), max_output_tokens=512, store=True,
        stream=True, context_management=[dict(type='compaction', compact_threshold=128)],
        reasoning=dict(effort='low', summary='concise'),
        include=['reasoning.encrypted_content'])
    return dict(automatic=auto,
        compact=dict(model=foreground.MODEL, input=history(),
            instructions='Preserve exact numeric facts. Write at most three short sentences.'),
        resumed=dict(common, input=[]), background_tool=tool,
        follow=dict(old['follow'], store=True),
        noop=dict(model=foreground.MODEL, input='Keep this user text unchanged.'),
        tampered=dict(common, input=[]), chat=old['chat'])


def compact_item(value):
    require(value.get('object') in ('response', 'response.compaction'), 'compact object')
    items = [item for item in value['output'] if item['type'] == 'compaction']
    require(len(items) == 1, 'one actual compaction item required')
    item = items[0]
    require(set(item) == {'type', 'id', 'encrypted_content'}, 'compact item fields')
    prefix = 'q4tc1:' + item['id'] + ':'
    require(item['encrypted_content'].startswith(prefix) and re.fullmatch(
        r'q4t1:[0-9a-f]{32}:[0-9a-f]{24}:[0-9a-f]{32}:(?:[0-9a-f]{2})+',
        item['encrypted_content'][len(prefix):]), 'actual authenticated compact state')
    return item


def check_reduction(before, resumed):
    original = before['prompt_tokens']
    compacted = resumed['usage']['input_tokens']
    require(type(original) is int and type(compacted) is int and
            0 < compacted < original, 'actual tokenizer did not show reduction')
    require(foreground.text(resumed).strip() == '42', 'important old fact was lost')
    return dict(original_prompt_tokens=original, restored_with_new_turn_tokens=compacted,
                saved_tokens=original-compacted)


def prepare(args):
    background.prepare(args, rows=requests(), limits=LIMITS, scope=__doc__)
    path = args.output.resolve()/'plan.json'
    plan = read(path)
    plan['planned'] = dict(starts=2, chats=8, internal_model_steps=MAX_MODEL_STEPS,
                           negative_before_execution=1, noop_model_steps=0)
    plan['mutations'] = ['only owned model, isolated synthetic client and object/key state',
        'graceful owned stop and one explicit restart in same strict GPU lease']
    plan['derived_requests'] = [
        'real tokenizer preflight of original compact source; no model generation',
        'restore actual opaque compact state plus new question; no hardcoded memory',
        'background explicit trigger produces actual document tool call; read docs/MODEL.md lines1-3',
        'persisted response and original SSE checked after graceful restart; no model replay',
        'real tool output and previous_response_id feed strict JSON document closure',
        'one modified ciphertext must fail400 before model execution; same-PID Chat follows',
        'no-op compact must report zero model input/output, single client admission',
        'actual cumulative tokens and RPM checked per process, no direct-engine RPM persistence claim']
    save(path, plan)


class Stage(encrypted.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_RESPONSES_COMPACTION_BUSINESS'

    def sdk(self, label, action, request=None, *, response_id=None, query=None,
            expected_status=None):
        self.budget('chats' if action in ('create', 'compact') else 'controls')
        command = dict(action=action, request=request, id=response_id, query=query,
            base_url=f"http://127.0.0.1:{self.plan['port']}/v1", headers=HEADERS,
            sdk_directory=self.plan['sdk']['sdk_directory'], timeout_ms=120000)
        save(self.out/(label+'.request.json'), command)
        env = {k: v for k, v in os.environ.items() if k not in
               ('NODE_OPTIONS', 'NODE_PATH', 'LD_PRELOAD')}
        run = subprocess.run(['node', str(ROOT/'tools/serve_responses/run_sdk.mjs')],
            input=json.dumps(command, ensure_ascii=False), capture_output=True,
            text=True, env=env, timeout=135)
        (self.out/(label+'.stderr.log')).write_text(run.stderr)
        require(not run.stderr and run.stdout, 'SDK bridge failed')
        result = json.loads(run.stdout)
        (self.out/(label+'.wire')).write_bytes(
            base64.b64decode(result.pop('wire_base64'), validate=True))
        save(self.out/(label+'.sdk.json'), result)
        require(len(result['exchanges']) == 1, 'SDK replayed request')
        if expected_status is not None:
            require(not result['ok'] and result['error']['status'] == expected_status,
                    'expected rejection absent')
        else:
            require(run.returncode == 0 and result['ok'],
                    'SDK failed: '+label+' '+str(result.get('error')))
            value = result['result']
            for event in result.get('chunks') or []:
                require(not errors(event, 'response_event'), 'event schema: '+event['type'])
            if value.get('object') == 'response.compaction':
                require(not errors(value, 'compacted_response'), 'compact schema')
                foreground.check_usage(value)
            elif value.get('object') == 'response':
                background.check_response(value, 'completed')
            if result.get('chunks'):
                encrypted.check_done_events(result)
        self.same_model()
        self.drained(label+'-drain')
        return result.get('result')

    def preflight(self):
        self.budget('controls')
        request = dict(model=foreground.MODEL, messages=history(),
                       enable_thinking=False, temperature=0, max_completion_tokens=384)
        status, raw = self.http('/v1/chat/preflight', dict(request=request,
            include_prompt=True, include_token_ids=True), label='original-preflight')
        require(status == 200, 'original source must fit actual tokenizer/context')
        return json.loads(raw)

    def business(self):
        rows = self.plan['requests']
        caps = read(self.out/'capabilities-1.json')
        require(caps['responses']['compaction']['enabled'], 'compact executor absent')
        original = self.preflight()
        automatic = self.sdk('automatic', 'create', rows['automatic'])
        compact_item(automatic)
        summary.check_summary(automatic, arithmetic=True)
        encrypted.encrypted_items(automatic)
        compact = self.sdk('standalone', 'compact', rows['compact'])
        item = compact_item(compact)
        preserved = [v for v in compact['output'] if v.get('role') == 'user']
        require([v['content'][0]['text'] for v in preserved] ==
                [v['content'] for v in rows['compact']['input'] if v['role'] == 'user'],
                'compact changed preserved user messages')
        resumed = copy.deepcopy(rows['resumed'])
        resumed['input'] = [copy.deepcopy(item), dict(role='user', content=
            'What was the verified result? Reply only with the decimal number.')]
        # The public contract permits omitted IDs. The opaque item must recover
        # its authenticated original ID, not synthesize a different identity.
        resumed['input'][0].pop('id')
        answer = self.sdk('opaque-resume', 'create', resumed)
        reduction = check_reduction(original, answer)
        save(self.out/'actual-reduction.json', reduction)
        tool = self.sdk('background-tool', 'create', rows['background_tool'])
        compact_item(tool)
        calls = [v for v in tool['output'] if v['type'] == 'function_call']
        require(len(calls) == 1 and calls[0]['status'] == 'completed' and
                calls[0]['name'] == 'read_project_record', 'actual document call absent')
        arguments = json.loads(calls[0]['arguments'])
        require(arguments == dict(path=foreground.DOCUMENT, start_line=1, line_count=3),
                'actual document arguments')
        result = foreground.base.ProjectRecords(ROOT).read(arguments)
        require(result['ok'] and result['source']['sha256'] ==
                self.plan['sources'][foreground.DOCUMENT], 'document identity')
        save(self.out/'actual-tool.json', dict(call=calls[0], result=result))
        self.accounting('before-restart', [automatic, compact, answer, tool])
        self.cleanup()
        cleanup = read(self.out/'cleanup.json')
        save(self.out/'first-cleanup.json', cleanup)
        require(cleanup['model_exit'] == 0, 'owned first model did not stop gracefully')
        self.model = None
        self.start()
        saved = self.sdk('background-get', 'retrieve', response_id=tool['id'])
        require({k: v for k, v in saved.items() if k != 'output_text'} == tool,
                'stored background compact output changed')
        replay = self.sdk('background-replay', 'retrieve', response_id=tool['id'],
                          query=dict(stream=True, include_obfuscation=False))
        require(replay == tool, 'original background SSE changed')
        first_events = read(self.out/'background-tool.sdk.json')['chunks']
        require(read(self.out/'background-replay.sdk.json')['chunks'] == first_events,
                'original event sequence was reconstructed or changed')
        follow = copy.deepcopy(rows['follow'])
        follow['previous_response_id'] = tool['id']
        follow['input'] = [dict(type='function_call_output', call_id=calls[0]['call_id'],
                               output=json.dumps(result, ensure_ascii=False))]
        final = self.sdk('document-follow', 'create', follow)
        actual = json.loads(foreground.text(final))
        title = result['source']['text'].splitlines()[0].split(': ', 1)[1]
        require(actual['title'] in (title, title.lstrip('# ')) and
                actual['citation'] == result['source']['citation'], 'document closure')
        noop = self.sdk('noop', 'compact', rows['noop'])
        compact_item(noop)
        require(noop['usage']['total_tokens'] == 0 and
                noop['output'][0]['content'][0]['text'] == rows['noop']['input'],
                'no-op invented model work or changed input')
        bad = copy.deepcopy(rows['tampered'])
        invalid = copy.deepcopy(item)
        opaque = invalid['encrypted_content']
        invalid['encrypted_content'] = opaque[:-1] + ('0' if opaque[-1] != '0' else '1')
        bad['input'] = [invalid, dict(role='user', content='Continue.')]
        self.sdk('tampered', 'create', bad, expected_status=400)
        require(read(self.out/'tampered.sdk.json')['error']['code'] ==
                'invalid_encrypted_content', 'wrong integrity failure')
        self.budget('chats')
        status, raw = self.http('/v1/chat/completions', rows['chat'], label='same-pid-chat')
        chat = json.loads(raw)
        require(status == 200 and chat['choices'][0]['finish_reason'] == 'stop' and
                chat['choices'][0]['message']['content'].strip() == '42', 'ordinary Chat')
        self.accounting('after-restart', [final, noop], chat=chat, rejected=1)
        logs = ''.join((self.out/f'model-{i}.log').read_text() for i in (1, 2))
        for value, steps in [(automatic, 3), (compact, 1), (answer, 1), (tool, 2),
                             (final, 1), (noop, 0)]:
            summary.check_steps(logs, value, steps)
        require(len(re.findall(r'^\[sampling\] request=', logs, re.M)) == MAX_MODEL_STEPS,
                'extra internal model work or replay')
        require('[mtp-accept] request='+chat['id'] in logs, 'ordinary MTP not exercised')
        save(self.out/'business-checks.json', dict(external_attempts=8,
            internal_model_steps=MAX_MODEL_STEPS, actual_tool_executions=1,
            actual_token_reduction=reduction, automatic_and_explicit_trigger=True,
            standalone_and_optional_id=True, reasoning_summary_encryption=True,
            original_background_replay_after_restart=True,
            no_op_zero_model_tokens=True, rejected_before_model=True,
            per_process_actual_usage=True, direct_engine_rpm_persistence_claim=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    command = modes.add_parser('prepare')
    command.add_argument('--binary', type=Path, required=True)
    command.add_argument('--output', type=Path, required=True)
    command.add_argument('--port', type=int, default=18987)
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

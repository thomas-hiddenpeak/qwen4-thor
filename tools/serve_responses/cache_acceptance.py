"""Actual native cache/prewarm/tool business: 1 start/10 requests/900s."""
import argparse
import copy
import json
import re
import sys
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_responses import acceptance as foreground
from tools.serve_responses import background_acceptance as background
from tools.serve_responses import encryption_acceptance as encrypted
from tools.serve_responses import summary_acceptance as summary
from tools.serve_service.package import read, require, save

LIMITS = dict(starts=1, chats=10, controls=2000, seconds=900)


def requests():
    archive = (ROOT / foreground.DOCUMENT).read_text()[:4200]
    record = ('The authoritative project code for this cache acceptance is 42.\n'
              'The archive is reference data; answer only the current question.\n')
    parts = [dict(type='input_text', text='Project archive:\n' + archive,
                  prompt_cache_breakpoint=dict(mode='explicit', ttl='12h')),
             dict(type='input_text', text='\nCurrent project record:\n' + record,
                  prompt_cache_breakpoint=dict(mode='explicit', ttl='30m')),
             dict(type='input_text', text='\nWhat is the authoritative project '
                  'code in the current project record? Reply only with its number.')]
    common = dict(model=foreground.MODEL, input=[dict(role='user', content=parts)],
                  reasoning=dict(effort='none'), temperature=0, store=True,
                  max_output_tokens=64, prompt_cache_key='acceptance:primary',
                  prompt_cache_options=dict(mode='explicit', ttl='12h'))
    prewarm = copy.deepcopy(common)
    prewarm['prompt_cache_options']['prewarm'] = True
    hit = copy.deepcopy(common)
    hit.update(stream=True, stream_options=dict(include_obfuscation=False))
    isolated = copy.deepcopy(common)
    isolated['prompt_cache_key'] = 'acceptance:other-key'
    disabled = copy.deepcopy(common)
    for part in disabled['input'][0]['content']:
        part.pop('prompt_cache_breakpoint', None)
    target = copy.deepcopy(common)
    target['text'] = dict(format=dict(type='json_schema', name='project_code',
        strict=True, schema=foreground.base.closed(dict(answer=dict(
            type='integer', const=42)))))
    target['input'][0]['content'][-1]['text'] = (
        '\nWhat is the authoritative project code? Return {"answer":the code}.')
    target_hit = copy.deepcopy(target)
    target_hit.update(background=True, stream=True,
                      stream_options=dict(include_obfuscation=False))
    chat = dict(model=foreground.MODEL, temperature=0, enable_thinking=False,
                max_completion_tokens=64, prompt_cache_key='acceptance:chat',
                prompt_cache_retention='24h', messages=[dict(role='user', content=
                    '\n'.join(part['text'] for part in parts))])
    old = foreground.requests()
    return dict(prewarm=prewarm, hit=hit, isolated=isolated, disabled=disabled,
                target=target, target_hit=target_hit, chat=chat,
                chat_hit=copy.deepcopy(chat), tool=old['tool'], follow=old['follow'])


def cache_usage(value, *, prewarm=False, mode=None, diagnostic=None,
                reason=None, structured=False):
    background.check_response(value, 'completed')
    usage = value['usage']
    cached = usage['input_tokens_details']['cached_tokens']
    written = usage['input_tokens_details']['cache_write_tokens']
    require(cached + written <= usage['input_tokens'], 'overlapping cache usage')
    if prewarm:
        require(value['output'] == [] and usage['output_tokens'] == 0 and
                usage['output_tokens_details']['reasoning_tokens'] == 0,
                'prewarm generated output')
    else:
        answer = foreground.text(value).strip()
        require((json.loads(answer) == dict(answer=42) if structured else
                 answer == '42') and usage['output_tokens'] > 0,
                'actual cached project answer')
    if mode == 'write':
        require(cached == 0 and written >= 1024, 'actual cold cache write missing')
    elif mode == 'hit':
        require(cached >= 1024 and written == 0, 'actual cache hit missing')
    elif mode == 'disabled':
        require(cached == 0 and written == 0, 'explicit empty mode cached input')
    if diagnostic is not None:
        item = value.get('prompt_cache_diagnostics') or {}
        require(item.get('type') == diagnostic, 'actual diagnostic type')
        if reason is not None:
            require(item.get('reason') == reason, 'actual diagnostic reason')
    return dict(input=usage['input_tokens'], read=cached, written=written,
                output=usage['output_tokens'])


def check_restore(log, value, layout, *, chat=False):
    matches = re.findall(r'\[session-cache\] request=' + re.escape(value['id']) +
        r' event=restore prefix=(\d+) ms=\S+ error=(\S+) source=(\w+) layout=(\w+)', log)
    details = value['usage']['prompt_tokens_details' if chat else 'input_tokens_details']
    require(len(matches) == 1 and matches[0] == (
        str(details['cached_tokens']), 'none', 'shared', layout),
        'actual cache restore does not support reported usage/layout')


def check_writes(log, value, *, expected=2):
    matches = re.findall(r'\[prefix-cache\] request=' + re.escape(value['id']) +
        r' event=write prefix=(\d+) ttl=(\d+) saved=(\d+) bytes=(\d+)', log)
    require(len(matches) == expected and all(row[2] == '1' for row in matches),
            'actual selected cache states did not persist')
    prefixes = [int(row[0]) for row in matches]
    require(prefixes == sorted(set(prefixes)) and prefixes[0] >= 1024,
            'explicit breakpoints not distinct actual token positions')
    require(max(prefixes) == value['usage']['input_tokens_details']['cache_write_tokens'],
            'reported write usage does not match actual saved coverage')
    require([int(row[1]) for row in matches] == [43200, 1800],
            'selected cache TTLs differ from requested breakpoints')


def check_chat(value, *, hit):
    require(value['choices'][0]['finish_reason'] == 'stop' and
            value['choices'][0]['message']['content'].strip() == '42',
            'actual standard Chat cache answer')
    usage = value['usage']
    require(usage['prompt_tokens'] >= 1024 and usage['completion_tokens'] > 0 and
            usage['total_tokens'] == usage['prompt_tokens'] + usage['completion_tokens'],
            'Chat cache usage')
    cached = usage['prompt_tokens_details']['cached_tokens']
    require((1024 <= cached <= usage['prompt_tokens']) if hit else cached == 0,
            'Chat actual hit/cold usage')


def prepare(args):
    background.prepare(args, rows=requests(), limits=LIMITS, scope=__doc__)
    path = args.output.resolve() / 'plan.json'
    plan = read(path)
    plan['planned'] = dict(starts=1, chats=10, responses=8, standard_chat=2,
                           prefill_only=1, decoding_requests=9)
    plan['mutations'] = ['one owned model using default uniform slot layout',
        'isolated client/object/cache state only; preserve accepted default',
        'no service/model restart, no cache corruption or other service control']
    plan['derived_requests'] = [
        'prefix is frozen actual docs/MODEL.md text plus explicit project record42',
        'comparison IDs come from prior real responses only',
        'prewarm must persist two actual states and emit zero output tokens',
        'MTP/target hit usage must equal actual successful restore prefix',
        'new key misses, explicit mode without markers reads/writes zero',
        'target background SSE replay keeps original committed events/diagnostics',
        'Chat standard key/retention write and hit with actual answer',
        'tool output is one actual read of docs/MODEL.md lines1-3',
        'no retry, no benchmark/full quality matrix, no throughput claim']
    save(path, plan)


class Stage(encrypted.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_RESPONSES_CACHE_BUSINESS'
    requests_per_minute = 16
    tokens_per_minute = 65536

    def chat(self, label, body, *, hit):
        self.budget('chats')
        save(self.out / (label + '.request.json'), body)
        status, raw = self.http('/v1/chat/completions', body, label=label, timeout=120)
        require(status == 200, 'actual Chat status: ' + raw[:256].decode(errors='replace'))
        value = json.loads(raw)
        check_chat(value, hit=hit)
        self.same_model()
        self.drained(label + '-drain')
        return value

    def business(self):
        rows = copy.deepcopy(self.plan['requests'])
        caps = read(self.out / 'capabilities-1.json')['responses']['prompt_cache']
        require(caps['enabled'] and caps['native_state'] and
                not caps['core_readiness_dependency'], 'cache capability missing')
        values = []
        prewarm = self.sdk('prewarm', 'create', rows['prewarm'])
        cache_usage(prewarm, prewarm=True, mode='write')
        values.append(prewarm)
        for label, mode, diagnostic, reason in (
                ('hit', 'hit', 'cache_hit', None),
                ('isolated', 'write', 'cache_miss', 'prompt_cache_key_changed'),
                ('disabled', 'disabled', 'unavailable', None)):
            rows[label]['prompt_cache_options']['comparison_response_id'] = prewarm['id']
            value = self.sdk(label, 'create', rows[label])
            cache_usage(value, mode=mode, diagnostic=diagnostic, reason=reason)
            values.append(value)
        target = self.sdk('target', 'create', rows['target'])
        cache_usage(target, mode='write', structured=True)
        values.append(target)
        rows['target_hit']['prompt_cache_options']['comparison_response_id'] = target['id']
        target_hit = self.sdk('target-hit', 'create', rows['target_hit'])
        cache_usage(target_hit, mode='hit', diagnostic='cache_hit', structured=True)
        values.append(target_hit)
        replay = self.sdk('target-replay', 'retrieve', response_id=target_hit['id'],
                          query=dict(stream=True, include_obfuscation=False))
        require(replay == target_hit and
                read(self.out / 'target-replay.sdk.json')['chunks'] ==
                read(self.out / 'target-hit.sdk.json')['chunks'],
                'original cache background events/diagnostics changed')
        chats = [self.chat('chat-write', rows['chat'], hit=False),
                 self.chat('chat-hit', rows['chat_hit'], hit=True)]
        tool = self.sdk('tool', 'create', rows['tool'])
        calls = [item for item in tool['output'] if item['type'] == 'function_call']
        require(len(calls) == 1 and calls[0]['status'] == 'completed' and
                calls[0]['name'] == 'read_project_record', 'actual document call missing')
        arguments = json.loads(calls[0]['arguments'])
        require(arguments == dict(path=foreground.DOCUMENT, start_line=1, line_count=3),
                'actual document arguments')
        result = foreground.base.ProjectRecords(ROOT).read(arguments)
        require(result['ok'] and result['source']['sha256'] ==
                self.plan['sources'][foreground.DOCUMENT], 'actual document identity')
        save(self.out / 'tool-execution.json', dict(call=calls[0], result=result))
        rows['follow'].update(previous_response_id=tool['id'], input=[dict(
            type='function_call_output', call_id=calls[0]['call_id'],
            output=json.dumps(result, ensure_ascii=False))])
        final = self.sdk('follow', 'create', rows['follow'])
        actual = json.loads(foreground.text(final))
        title = result['source']['text'].splitlines()[0].split(': ', 1)[1]
        require(actual['title'] in (title, title.lstrip('# ')) and
                actual['citation'] == result['source']['citation'], 'document closure')
        values.extend([tool, final])
        metrics = self.drained('final')
        expected_input = sum(v['usage']['input_tokens'] for v in values) + sum(
            v['usage']['prompt_tokens'] for v in chats)
        expected_output = sum(v['usage']['output_tokens'] for v in values) + sum(
            v['usage']['completion_tokens'] for v in chats)
        key = '{client="' + encrypted.CLIENT + '"}'
        require(metrics['q4t_requests_total'] == 10 and
                metrics['q4t_requests_success_total'] == 10 and
                metrics['q4t_requests_error_total'] == 0 and
                metrics['q4t_prompt_tokens_total'] == expected_input and
                metrics['q4t_generation_tokens_total'] == expected_output,
                'terminal/actual usage accounting')
        require(metrics['q4t_client_rate_admitted_total' + key] == 10 and
                metrics['q4t_client_rate_reserved_tokens' + key] == 0 and
                metrics['q4t_client_rate_settled_tokens_total' + key] ==
                expected_input + expected_output, 'actual client quota accounting')
        log = (self.out / 'model-1.log').read_text()
        for value in values + chats:
            summary.check_steps(log, value, 1)
        require(len(re.findall(r'^\[sampling\] request=', log, re.M)) == 10,
                'extra internal request/model step')
        check_writes(log, prewarm)
        check_writes(log, values[2])
        check_writes(log, target)
        check_restore(log, values[1], 'mtp')
        check_restore(log, target_hit, 'target')
        check_restore(log, chats[1], 'mtp', chat=True)
        require('[mtp-accept] request=' + chats[1]['id'] in log,
                'standard cached Chat did not execute MTP')
        save(self.out / 'business-checks.json', dict(external_requests=10,
            prefill_only_requests=1, actual_tool_executions=1,
            successful_shared_layouts=['mtp', 'target'], explicit_breakpoints=2,
            exact_cache_usage=True, exact_client_usage=True,
            key_isolation=True, explicit_no_marker_no_cache=True,
            original_background_replay=True, default_uniform_slots=True,
            performance_claim=False, cross_restart_cache_claim=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    command = modes.add_parser('prepare')
    command.add_argument('--binary', type=Path, required=True)
    command.add_argument('--output', type=Path, required=True)
    command.add_argument('--port', type=int, default=18991)
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

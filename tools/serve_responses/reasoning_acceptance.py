"""Actual pro: cumulative 2 starts/9 requests/17 model steps/900s after driver fix."""
import argparse
import json
from pathlib import Path
import re
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_responses import acceptance as foreground
from tools.serve_responses import background_acceptance as background
from tools.serve_responses import cache_acceptance as cache
from tools.serve_responses import compaction_acceptance as compact
from tools.serve_responses import encryption_acceptance as encrypted
from tools.serve_responses import summary_acceptance as summary
from tools.serve_service.package import read, require, save, sha

LIMITS = dict(starts=2, chats=9, controls=2000, seconds=900)
MAX_MODEL_STEPS = 17


def requests():
    old = foreground.requests()
    common = dict(model=foreground.MODEL, temperature=0, store=True,
        max_output_tokens=384, reasoning=dict(mode='pro', effort='none'))
    arithmetic = dict(common, input=old['plain']['input'])
    structured = dict(arithmetic, text=dict(format=dict(type='json_schema',
        name='sum', strict=True, schema=foreground.base.closed(dict(
            answer=dict(type='integer', const=42))))))
    streamed = dict(arithmetic, max_output_tokens=512,
        reasoning=dict(mode='pro', effort='low', summary='concise'),
        stream=True, stream_options=dict(include_obfuscation=False))
    stored = dict(structured, background=True, stream=True,
        stream_options=dict(include_obfuscation=False),
        include=['reasoning.encrypted_content'])
    tool = dict(old['tool'], **common)
    follow = dict(old['follow'], **common)
    tiny = dict(arithmetic, max_output_tokens=1, stream=True,
                stream_options=dict(include_obfuscation=False))
    prewarm = cache.requests()['prewarm']
    prewarm['reasoning'] = dict(mode='pro', effort='none')
    compacted = dict(arithmetic, input=compact.history(),
        max_output_tokens=512, stream=True,
        stream_options=dict(include_obfuscation=False),
        context_management=[dict(type='compaction', compact_threshold=128)])
    return dict(structured=structured, summary=streamed, background=stored,
        tool=tool, follow=follow, tiny=tiny, prewarm=prewarm,
        compacted=compacted, chat=old['chat'])


def check_pro(value, *, answer=None, incomplete=False):
    background.check_response(value, 'incomplete' if incomplete else 'completed')
    require(value['reasoning']['mode'] == 'pro', 'pro selection lost')
    usage = value['usage']
    require(0 < usage['output_tokens_details']['reasoning_tokens'] <=
            usage['output_tokens'] <= value['max_output_tokens'],
            'actual deliberation/shared budget usage absent')
    notes = ''.join(part['text'] for item in value['output']
        if item['type'] == 'reasoning' for part in item.get('content', [])
        if part['type'] == 'reasoning_text')
    if incomplete:
        require(value['incomplete_details']['reason'] == 'max_output_tokens' and
                usage['output_tokens'] == 1 and not foreground.text(value) and
                all(item['type'] == 'reasoning' for item in value['output']),
                'tiny budget refilled or fabricated final answer')
    else:
        require(notes.strip(), 'pro lacks actual working notes')
    if answer is not None:
        text = foreground.text(value).strip()
        require((json.loads(text) == answer if isinstance(answer, dict) else text == answer),
                'pro final task answer differs')


def check_execution(log, values, chat):
    expected = dict(structured=2, summary=3, background=2, tool=2,
                    follow=2, tiny=1, prewarm=1, compacted=3)
    for name, count in expected.items():
        summary.check_steps(log, values[name], count)
    summary.check_steps(log, chat, 1)
    require(sum(expected.values()) + 1 == MAX_MODEL_STEPS and
            len(re.findall(r'^\[sampling\] request=', log, re.M)) == MAX_MODEL_STEPS,
            'extra generation or replay outside fixed model-step budget')


def prepare(args):
    prior = None
    if args.resume_from:
        directory = args.resume_from.resolve(strict=True)
        result = read(directory / 'result.json')
        cleanup = read(directory / 'cleanup.json')
        old = read(directory / 'plan.json')
        require(result['status'] == 'FAILED' and result['counts']['starts'] == 1 and
                result['counts']['chats'] == 0 and
                'Address already in use' in result['error'] and
                cleanup['model_exit'] == 0 and not cleanup['remaining'] and
                cleanup['ports_released'], 'not the known pre-request driver failure')
        require(not re.search(r'^\[sampling\]', (directory / 'model-1.log').read_text(), re.M),
                'first run unexpectedly generated output')
        changes = {name for name, digest in old['sources'].items() if sha(ROOT/name) != digest}
        require(changes <= {'tools/serve_responses/reasoning_acceptance.py',
                            'tools/serve_responses/test_reasoning_acceptance.py'},
                'production source changed after first run')
        require(sha(args.binary.resolve(strict=True)) == old['binary_sha256'],
                'actual candidate changed')
        samples = [json.loads(line) for line in
                   (directory / 'lease/nvml-queries.jsonl').read_text().splitlines()]
        require(samples and all(row['status'] == 'OK' for row in samples),
                'prior monitoring did not remain healthy')
        processes = foreground.base.lifecycle.guard.processes()
        require(not any(processes.get(int(pid), {}).get('start_ticks') == item['start_ticks']
                        for pid, item in cleanup['owned'].items()), 'prior process survives')
        prior = dict(directory=str(directory), counts=result['counts'], seconds=result['seconds'],
            files={str(path): sha(path) for path in directory.rglob('*') if path.is_file()},
            source_changes=sorted(changes), completed_generations=0)
    background.prepare(args, rows=requests(), limits=LIMITS, scope=__doc__)
    path = args.output.resolve() / 'plan.json'
    plan = read(path)
    plan['planned'] = dict(starts=2 if prior else 1, chats=9, maximum_model_steps=MAX_MODEL_STEPS,
                           actual_tool_executions=1, prefill_only=1)
    if prior:
        plan['prior'] = prior
    plan['mutations'] = ['only one owned model and isolated test client/object/cache state',
                         'no restart or interference with accepted default/other services']
    plan['derived_requests'] = [
        'pro actual deliberation then original JSON/tool contract, effort independent',
        'actual summary consumes same output budget after deliberation and final answer',
        'background original SSE/ciphertext and persistent input replay; no new execution',
        'one real read of docs/MODEL.md lines1-3; original tool ID and result used for continuation',
        'one-token pro ends incomplete without replenishing budget or creating final answer',
        'prewarm skips deliberation, writes actual cache, zero generated tokens',
        'actual automatic compaction precedes pro and preserves unique old numeric fact',
        'one standard same-PID Chat and exact aggregate input/output/client accounting',
        'actual model log counts enforce total17 steps; no retry, no performance claim']
    save(path, plan)


class Stage(encrypted.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_RESPONSES_PRO_BUSINESS'
    requests_per_minute = 16
    tokens_per_minute = 65536

    def __init__(self, plan):
        super().__init__(plan)
        if plan.get('prior'):
            prior = plan['prior']
            require(all(sha(path) == digest for path, digest in prior['files'].items()),
                    'prior failure evidence changed')
            self.counts.update(prior['counts'])
            self.started -= prior['seconds']

    def business(self):
        # The shared Stage.run owns the single start and eventual cleanup.
        generation = self.counts['starts']
        caps = read(self.out / f'capabilities-{generation}.json')['responses']['reasoning_execution']
        require(caps['pro'] == 'native_deliberation_then_answer' and
                caps['shared_output_budget'] and not caps['speculative_tool_execution'],
                'actual capability contract')
        rows, values = requests(), {}
        for name in ('structured', 'summary', 'background'):
            value = self.sdk(name, 'create', rows[name])
            check_pro(value, answer='42' if name == 'summary' else dict(answer=42))
            values[name] = value
        summary.check_summary(values['summary'], arithmetic=True)
        encrypted.encrypted_items(values['background'])
        replay = self.sdk('background-replay', 'retrieve',
            response_id=values['background']['id'], query=dict(stream=True,
                include_obfuscation=False, include=['reasoning.encrypted_content']))
        require(replay == values['background'] and
                read(self.out / 'background-replay.sdk.json')['chunks'] ==
                read(self.out / 'background.sdk.json')['chunks'],
                'original background events changed or regenerated')
        original = self.sdk('background-input', 'input_items',
                            response_id=values['background']['id'])
        require(len(original['data']) == 1 and
                rows['background']['input'] in json.dumps(original, ensure_ascii=False),
                'temporary pro notes leaked into original input history')
        tool = self.sdk('tool', 'create', rows['tool'])
        check_pro(tool)
        calls = [item for item in tool['output'] if item['type'] == 'function_call']
        require(len(calls) == 1 and calls[0]['status'] == 'completed' and
                calls[0]['name'] == 'read_project_record', 'one completed tool call required')
        arguments = json.loads(calls[0]['arguments'])
        require(arguments == dict(path=foreground.DOCUMENT, start_line=1, line_count=3),
                'actual document tool arguments')
        result = foreground.base.ProjectRecords(ROOT).read(arguments)
        require(result['ok'] and result['source']['sha256'] ==
                self.plan['sources'][foreground.DOCUMENT], 'actual document source')
        save(self.out / 'tool-execution.json', dict(call=calls[0], result=result))
        rows['follow'].update(previous_response_id=tool['id'], input=[dict(
            type='function_call_output', call_id=calls[0]['call_id'],
            output=json.dumps(result, ensure_ascii=False))])
        final = self.sdk('follow', 'create', rows['follow'])
        check_pro(final)
        actual = json.loads(foreground.text(final))
        title = result['source']['text'].splitlines()[0].split(': ', 1)[1]
        require(actual['title'] in (title, title.lstrip('# ')) and
                actual['citation'] == result['source']['citation'], 'real document closure')
        values.update(tool=tool, follow=final)
        tiny = self.sdk('tiny', 'create', rows['tiny'])
        check_pro(tiny, incomplete=True)
        values['tiny'] = tiny
        prewarm = self.sdk('prewarm', 'create', rows['prewarm'])
        cache.cache_usage(prewarm, prewarm=True, mode='write')
        values['prewarm'] = prewarm
        compacted = self.sdk('compacted', 'create', rows['compacted'])
        check_pro(compacted, answer='42')
        compact.compact_item(compacted)
        values['compacted'] = compacted
        self.budget('chats')
        status, raw = self.http('/v1/chat/completions', rows['chat'], label='same-pid-chat')
        chat = json.loads(raw)
        require(status == 200 and chat['choices'][0]['finish_reason'] == 'stop' and
                chat['choices'][0]['message']['content'].strip() == '42',
                'ordinary same-PID Chat failed')
        self.accounting('final-accounting', list(values.values()), chat=chat)
        log = (self.out / f'model-{generation}.log').read_text()
        check_execution(log, values, chat)
        cache.check_writes(log, prewarm)
        require('[mtp-accept] request=' + chat['id'] in log, 'ordinary MTP path not exercised')
        save(self.out / 'business-checks.json', dict(external_requests=9,
            actual_model_steps=MAX_MODEL_STEPS, actual_tool_executions=1,
            json_sse_background=True, original_input_and_sse=True,
            summary_encryption_compaction_combination=True,
            exact_shared_budget_and_client_accounting=True,
            tiny_incomplete_no_answer=True, prewarm_no_deliberation=True,
            default_unchanged=True, performance_claim=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    command = modes.add_parser('prepare')
    command.add_argument('--binary', type=Path, required=True)
    command.add_argument('--output', type=Path, required=True)
    command.add_argument('--port', type=int, default=18993)
    command.add_argument('--resume-from', type=Path)
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

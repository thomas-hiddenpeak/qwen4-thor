"""One start/twelve requests: real stop, complete values and private state reuse."""
import argparse
import base64
import copy
from dataclasses import asdict
import hashlib
import io
import json
from pathlib import Path
import re
import signal
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_protocol import media_cache_acceptance as media
from tools.serve_service.package import read, require, save, sha
from tools.examples.chat_client import RemoteError, read_json, read_sse

LIMITS = dict(starts=1, chats=12, controls=2000, seconds=600)
MEDIA_LIMITS = dict(starts=1, chats=3, controls=1000, seconds=360)
DOCUMENT = media.DOCUMENT


def requests():
    base = dict(model=media.responses.MODEL, temperature=0,
        reasoning_effort='none', max_completion_tokens=128,
        prompt_cache_options=dict(mode='explicit'),
        messages=[dict(role='user', content='Write exactly: ALPHA END OMEGA')])
    rows = {}
    rows['none_json'] = dict(copy.deepcopy(base), tool_choice='none', stop=' END')
    rows['none_sse'] = dict(copy.deepcopy(rows['none_json']), stream=True,
                          stream_options=dict(include_usage=True))
    schema = dict(type='object', properties=dict(answer=dict(type='string',
        const='ALPHA')), required=['answer'], additionalProperties=False)
    rows['schema_complete'] = dict(copy.deepcopy(base), stop='NEVER_END_92481',
        response_format=dict(type='json_schema', json_schema=dict(
            name='answer', strict=True, schema=schema)))
    rows['schema_complete']['messages'][0]['content'] = 'Return {"answer":"ALPHA"}.'
    rows['schema_cut'] = dict(copy.deepcopy(rows['schema_complete']), stop='answer',
                            stream=True, stream_options=dict(include_usage=True))
    tool = dict(type='function', function=dict(name='project_document', strict=True,
        description='Read the named real project document.', parameters=dict(
            type='object', properties=dict(path=dict(type='string', const=DOCUMENT)),
            required=['path'], additionalProperties=False)))
    rows['tool_complete'] = dict(copy.deepcopy(base), tools=[tool],
        tool_choice='required', parallel_tool_calls=False, stop='NEVER_END_92481')
    rows['tool_complete']['messages'][0]['content'] = 'Read '+DOCUMENT+' using project_document.'
    rows['tool_cut'] = dict(copy.deepcopy(rows['tool_complete']), stop='<parameter=',
                          stream=True, stream_options=dict(include_usage=True))
    rows['private_write'] = dict(copy.deepcopy(base), session_id='d'*64, stop=' END')
    rows['private_write']['messages'][0]['content'] = (
        'Archived context record.\n'*2200 +
        '\nIgnore the repetitive archive. Write exactly: ALPHA END OMEGA')
    rows['private_follow'] = copy.deepcopy(rows['private_write'])
    rows['private_uncached'] = copy.deepcopy(rows['private_write'])
    rows['private_uncached'].pop('session_id')
    image = 'data:image/png;base64,' + base64.b64encode(
        (ROOT/media.FILES[0]).read_bytes()).decode()
    rows['media_write'] = dict(copy.deepcopy(rows['schema_complete']),
        session_id='e'*64, logprobs=True, top_logprobs=3)
    rows['media_write']['response_format']['json_schema'] = dict(
        name='colors', strict=True, schema=dict(type='object',
            properties=dict(visible_colors=dict(type='boolean')),
            required=['visible_colors'], additionalProperties=False))
    rows['media_write']['messages'] = [dict(role='user', content=[
        dict(type='text', text='Archive record.\n'*2800),
        dict(type='image_url', image_url=dict(url=image, detail='auto')),
        dict(type='text', text='Return JSON: visible_colors is true if the '
             'image has multiple colors.')])]
    rows['media_follow'] = copy.deepcopy(rows['media_write'])
    rows['media_uncached'] = copy.deepcopy(rows['media_write'])
    rows['media_uncached'].pop('session_id')
    for label, row in rows.items():
        row.update(request_id='stop-combo-'+label,
                   cancel_token=hashlib.sha256(label.encode()).hexdigest())
    return rows


def media_requests():
    return {k: v for k, v in requests().items() if k.startswith('media_')}


class Stage(media.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_STOP_COMBINATIONS_BUSINESS'
    baseline_first = False
    initial_labels = ('none_json', 'none_sse', 'schema_complete', 'schema_cut',
                      'tool_complete', 'tool_cut')
    state_kinds = ('private', 'media')

    def chat(self, label, body, *, failure=False):
        self.budget('controls')
        status, raw = self.http('/v1/chat/preflight', dict(request=body,
            include_prompt=True, include_token_ids=True), label=label+'-preflight')
        require(status == 200, 'valid combination rejected: '+label)
        save(self.out/(label+'-preflight.json'), json.loads(raw))
        self.budget('chats')
        save(self.out/(label+'.request.json'), body)
        status, raw = self.http('/v1/chat/completions', body, label=label, timeout=180)
        if failure:
            require(status in (200, 502), 'unexpected error status: '+label)
            error = None
            if status == 502:
                error = json.loads(raw)['error']
            else:
                try:
                    read_sse(io.BytesIO(raw))
                except RemoteError as caught:
                    error = dict(code=caught.code)
                events = [json.loads(line[5:]) for line in raw.splitlines()
                          if line.startswith(b'data:') and
                          line[5:].strip() != b'[DONE]']
                require(not any(choice.get('finish_reason') is not None
                    for event in events for choice in event.get('choices', [])),
                    'failed constraint published a success terminal')
            require(error and error.get('code') == 'structured_output_invalid',
                    'missing final validation failure: '+label)
            save(self.out/(label+'.expected-error.json'), error)
            value = None
        else:
            require(status == 200, 'business HTTP failed: '+label)
            value = (read_sse if body.get('stream') else read_json)(io.BytesIO(raw))
            save(self.out/(label+'.result.json'), asdict(value))
        self.same_model()
        self.drained(label+'-drain')
        require(self.http('/readyz')[0] == 200, 'request failure closed readiness')
        return value

    def business(self):
        caps = read(self.out/'candidate-capabilities.json')['text_stop']
        require(caps['profile'] == 'q4t-text-stop-v2' and
                caps['state_cache'] and caps['constrained_output'], 'capability absent')
        rows = self.plan['requests']
        values = {}
        for label in self.initial_labels:
            value = self.chat(label, rows[label], failure=label.endswith('_cut'))
            if value is not None:
                values[label] = value
                if label == 'tool_complete':
                    calls = value.message.get('tool_calls', [])
                    require(value.finish_reason == 'tool_calls' and len(calls) == 1 and
                        calls[0]['function']['name'] == 'project_document' and
                        json.loads(calls[0]['function']['arguments']) == dict(path=DOCUMENT),
                        'required tool output differs')
                    require(sha(ROOT/DOCUMENT) == self.plan['sources'][DOCUMENT],
                            'actual project source drift')
                    save(self.out/'actual-tool-execution.json', dict(path=DOCUMENT,
                        sha256=sha(ROOT/DOCUMENT), content=(ROOT/DOCUMENT).read_text()))
                else:
                    content = value.message['content']
                    require(value.finish_reason == 'stop' and
                        (json.loads(content) == dict(answer='ALPHA')
                         if label == 'schema_complete' else content.strip() == 'ALPHA'),
                        'actual stopped/structured answer differs: '+label)
        for kind in self.state_kinds:
            label = kind+'_write'
            first = self.chat(label, rows[label])
            values[label] = first
            require(first.finish_reason == 'stop' and
                    isinstance(first.message.get('content'), str),
                    'requested structured/text answer was not produced: '+kind)
            correct_answer = (first.message['content'].strip() == 'ALPHA'
                if kind == 'private' else
                json.loads(first.message['content']) == dict(visible_colors=True))
            require(first.usage['prompt_tokens'] >= 8192 and correct_answer,
                    'private prompt or answer: '+kind)
            for suffix in ('follow', 'uncached'):
                label = kind+'_'+suffix
                body = copy.deepcopy(rows[label])
                body['messages'] += [first.message, dict(role='user', content=(
                    'Write exactly: BETA END OMEGA' if kind == 'private' else
                    'Inspect the same image again. Return JSON with '
                    'visible_colors indicating whether it has multiple colors.'))]
                if suffix == 'follow':
                    body.update(stream=True, stream_options=dict(include_usage=True))
                values[label] = self.chat(label, body)
                value = values[label]
                require(value.finish_reason == 'stop' and
                    (value.message['content'].strip() == 'BETA' if kind == 'private'
                     else json.loads(value.message['content']) == dict(visible_colors=True)),
                    'actual continuation differs: '+label)
                cached = value.usage.get('prompt_tokens_details', {}).get('cached_tokens', 0)
                require(cached == (first.usage['prompt_tokens'] if suffix == 'follow' else 0),
                        'not the exact prompt recovery boundary: '+label)
            require(values[kind+'_follow'].message == values[kind+'_uncached'].message,
                    'cached/uncached response differs')
            if kind == 'media':
                require(values['media_follow'].logprobs == values['media_uncached'].logprobs,
                        'visual target cache probabilities differ')
        log = (self.out/'candidate-model.log').read_text()
        proof = {}
        for kind in self.state_kinds:
            layout = 'mtp' if kind == 'private' else 'target'
            for suffix in ('write', 'follow'):
                label = kind+'_'+suffix
                entries = re.findall(r'\[session-cache\] request='+
                    re.escape(rows[label]['request_id'])+
                    r' event=save prefix=(\d+) ms=\S+ disk_bytes=(\d+) error=(\S+) layout=(\w+) source=(\w+)', log)
                require(len(entries) == 1 and entries[0][2:] ==
                    ('none', layout, 'private_prompt') and int(entries[0][1]) > 0 and
                    int(entries[0][0]) == values[label].usage['prompt_tokens'],
                    'hidden output tail was saved or prompt save failed: '+label)
                proof[label] = entries
            matches = re.findall(r'\[session-cache\] request='+
                re.escape(rows[kind+'_follow']['request_id'])+
                r' event=restore prefix=(\d+) ms=\S+ error=(\S+) source=(\w+) layout=(\w+)', log)
            require(matches == [(str(values[kind+'_write'].usage['prompt_tokens']),
                    'none', 'session', layout)], 'actual restore differs: '+kind)
        # The continuation may itself end at EOS after BETA. Its recovery is
        # checked above; only actual matched stops are claimed as such.
        for label in ('none_json', 'none_sse', 'private_write'):
            if label not in rows:
                continue
            require(re.search(r'\[text-stop\] request='+re.escape(rows[label]['request_id'])+
                    r' matched_index=0 ', log), 'stop was not hit: '+label)
        if 'private' in self.state_kinds:
            require('[mtp-accept] request='+rows['private_write']['request_id'] in log,
                    'text private scenario did not execute MTP')
        metrics = self.drained('final')
        expected = len(rows)
        errors = sum(label.endswith('_cut') for label in self.initial_labels)
        require(self.counts['chats'] == expected and
            metrics['q4t_requests_total'] == expected and
            metrics['q4t_requests_success_total'] == expected-errors and
            metrics['q4t_requests_error_total'] == errors, 'terminal accounting differs')
        save(self.out/'business-checks.json', dict(requests=expected,
            expected_constraint_errors=errors,
            real_tool_executions=int('tool_complete' in rows),
            same_healthy_model=True, exact_prompt_snapshots=proof,
            original_gpu_numerical_evidence_reused=True, performance_claim=False))


class MediaStage(Stage):
    limits = MEDIA_LIMITS
    request_templates = staticmethod(media_requests)
    initial_labels = ()
    state_kinds = ('media',)
    success_status = 'PASS_STOP_MEDIA_COMBINATIONS_BUSINESS'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['prepare', 'run'])
    parser.add_argument('--binary', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--port', type=int, default=18772)
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--media-only', action='store_true')
    args = parser.parse_args()
    if args.mode == 'prepare':
        media.responses.base.prepare(args,
            rows=media_requests() if args.media_only else requests(),
            limits=MEDIA_LIMITS if args.media_only else LIMITS,
            scope=('Only the unfinished three media stop/schema/private-cache '
                   'requests, using actual image content; prior failure retained.'
                   if args.media_only else __doc__))
        path = args.output/'plan.json'
        plan = read(path)
        baseline = (ROOT/'build/q4t').resolve(strict=True)
        plan['baseline'] = dict(binary=str(baseline), sha256=sha(baseline))
        for name in (DOCUMENT, *media.FILES,
                     'tools/serve_protocol/media_cache_acceptance.py',
                     'tools/serve_protocol/stop_combinations_acceptance.py'):
            plan['sources'][name] = sha(ROOT/name)
        plan['media_only'] = args.media_only
        plan['planned'] = dict(starts=1, requests=3 if args.media_only else 12,
            expected_errors=0 if args.media_only else 2,
            private_cache_minimum=8192,
            real_tool_execution=0 if args.media_only else 1,
            exact_stop_match=not args.media_only)
        save(path, plan)
    else:
        def deadline(*_):
            raise TimeoutError('stop combination business deadline')
        signal.signal(signal.SIGALRM, deadline)
        plan = read(args.plan)
        signal.setitimer(signal.ITIMER_REAL, plan['limits']['seconds']-50)
        with media.responses.base.lifecycle.child_subreaper():
            (MediaStage if plan.get('media_only') else Stage)(plan).run()


if __name__ == '__main__':
    main()

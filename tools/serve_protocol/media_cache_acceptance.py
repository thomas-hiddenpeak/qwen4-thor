"""Two starts/22 requests: visual chunk continuity and real SSD cache identity.

The baseline is an immutable accepted binary. It runs two uncached one-shot
prompts; the candidate uses 512-row chunks and executes the 20 related cases.
This is functional acceptance, with exact same-boundary cache output checks,
not a throughput experiment. The outer driver must own the strict GPU lease.
"""
import argparse
import base64
import copy
from dataclasses import asdict
import hashlib
import http.client
import io
import json
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_protocol import chat_cache_acceptance as text_cache
from tools.serve_responses import acceptance as responses
from tools.serve_responses import encryption_acceptance as encrypted
from tools.serve_service.package import read, require, save, sha
from tools.examples.chat_client import read_json, read_sse

LIMITS = dict(starts=2, chats=22, controls=3000, seconds=720)
PRIVATE_LIMITS = dict(starts=1, chats=2, controls=1500, seconds=360)
FILES = ('tests/fixtures/file_images/original.png',
         'tests/fixtures/file_images/gif/opaque.png')
DOCUMENT = 'docs/SERVE_MEMORY_STOP_2026-11-03.md'


def check_tool_reply(content, digest):
    # The request asks to report a digest, not to suppress surrounding prose.
    # Require its exact unique 12-digit value, with no competing digest.
    require(re.findall(r'\b[0-9a-f]{12}\b', content) == [digest[:12]],
            'actual tool answer')


def private_requests():
    return {k: v for k, v in requests().items() if k.startswith('private_')}


def tagged(label, body):
    result = copy.deepcopy(body)
    result.update(request_id='media-state-' + label,
                  cancel_token=hashlib.sha256(label.encode()).hexdigest())
    return result


def requests():
    urls = ['data:image/png;base64,' + base64.b64encode(
        (ROOT / name).read_bytes()).decode() for name in FILES]
    archive = 'Project record for cache continuity.\n' * 200
    image = dict(type='image_url', image_url=dict(url=urls[0], detail='auto'))
    video = dict(type='video_frames', video_frames=[urls[0], urls[1], urls[0]],
                 video_timestamps=[0, .25, .5])

    def request(part, kind, marker):
        part = copy.deepcopy(part)
        if marker:
            part['prompt_cache_breakpoint'] = dict(mode='explicit', ttl='30m')
        question = ('Does the image contain multiple colors? Reply only YES or NO.'
                    if kind == 'image' else
                    'Do these video frames contain multiple colors? Reply only YES or NO.')
        return dict(model=responses.MODEL, temperature=0, reasoning_effort='none',
            max_completion_tokens=64, logprobs=True, top_logprobs=3,
            prompt_cache_key='media-state-' + kind,
            prompt_cache_options=dict(mode='explicit', ttl='30m'),
            messages=[dict(role='user', content=[dict(type='text', text=archive),
                part, dict(type='text', text=question)])])

    rows = {}
    for kind, part in (('image', image), ('video', video)):
        for suffix in ('uncached', 'write', 'hit', 'changed'):
            rows[kind + '_' + suffix] = request(part, kind, suffix != 'uncached')
        rows['base_' + kind] = request(part, kind, False)
        rows[kind + '_hit'].update(stream=True, stream_options=dict(include_usage=True))
    rows['image_changed']['messages'][0]['content'][1]['image_url']['url'] = urls[1]
    rows['video_changed']['messages'][0]['content'][1]['video_frames'][-1] = urls[1]
    for label in ('image_key', 'image_client', 'image_append'):
        rows[label] = copy.deepcopy(rows['image_write'])
    rows['image_key']['prompt_cache_key'] = 'media-state-other'
    rows['image_append']['messages'][0]['content'].insert(2, copy.deepcopy(image))
    rows['image_append']['messages'][0]['content'][-1]['text'] = (
        'Are both images multicolored? Reply only YES or NO.')
    rows['video_timestamp'] = copy.deepcopy(rows['video_write'])
    rows['video_timestamp']['messages'][0]['content'][1]['video_timestamps'] = [1, 2, 3]

    schema = dict(type='object', properties=dict(visible_colors=dict(type='boolean')),
                  required=['visible_colors'], additionalProperties=False)
    for label in ('schema_write', 'schema_hit'):
        rows[label] = copy.deepcopy(rows['image_write'])
        rows[label]['prompt_cache_key'] = 'media-state-schema'
        rows[label]['messages'][0]['content'][-1]['text'] = (
            'Return JSON: visible_colors is true if the image has multiple colors.')
        rows[label]['response_format'] = dict(type='json_schema', json_schema=dict(
            name='colors', strict=True, schema=schema))
    rows['schema_hit'].update(stream=True, stream_options=dict(include_usage=True))
    response_content = [dict(type='input_text', text=archive),
        dict(type='input_image', image_url=urls[0], detail='auto',
             prompt_cache_breakpoint=dict(mode='explicit', ttl='30m')),
        dict(type='input_text', text=rows['image_write']['messages'][0]['content'][-1]['text'])]
    for label in ('responses_write', 'responses_hit'):
        rows[label] = dict(model=responses.MODEL, temperature=0,
            reasoning=dict(effort='none'), max_output_tokens=64, store=False,
            input=[dict(role='user', content=copy.deepcopy(response_content))],
            prompt_cache_key='media-state-responses',
            prompt_cache_options=dict(mode='explicit', ttl='30m'))
    rows['responses_hit']['stream'] = True

    tool = dict(type='function', function=dict(name='project_document', strict=True,
        description='Read a real project document and return its SHA256.',
        parameters=dict(type='object', properties=dict(path=dict(type='string', const=DOCUMENT)),
                        required=['path'], additionalProperties=False)))
    rows['tool_call'] = copy.deepcopy(rows['image_write'])
    rows['tool_call'].update(tools=[tool], tool_choice='required',
                            parallel_tool_calls=False, max_completion_tokens=128)
    rows['tool_call']['messages'][0]['content'][-1]['text'] = (
        'Use project_document to read ' + DOCUMENT + '. Then report its actual SHA256 first 12 digits.')
    rows['tool_follow'] = copy.deepcopy(rows['tool_call'])
    rows['tool_follow']['tool_choice'] = 'none'
    rows['private_write'] = request(image, 'image', False)
    rows['private_write']['messages'][0]['content'][0]['text'] = 'archive record\n' * 2800
    rows['private_write']['session_id'] = 'c' * 64
    rows['private_follow'] = copy.deepcopy(rows['private_write'])
    require(len(rows) == 22, 'frozen request count')
    return {label: tagged(label, row) for label, row in rows.items()}


def prepare(args):
    private = args.private_only
    responses.base.prepare(args, rows=private_requests() if private else requests(),
        limits=PRIVATE_LIMITS if private else LIMITS, scope=(
            'Only the two unfinished private media session requests; no replay of passed cases.'
            if private else __doc__))
    path = args.output.resolve() / 'plan.json'
    plan = read(path)
    baseline = args.baseline.resolve(strict=True)
    require(baseline != Path(plan['binary']), 'distinct accepted baseline required')
    plan['baseline'] = dict(binary=str(baseline), sha256=sha(baseline))
    for name in (*FILES, DOCUMENT):
        plan['sources'][name] = sha(ROOT / name)
    for name in subprocess.check_output(['git', 'ls-files', 'tools/serve_responses'],
            cwd=ROOT, text=True).splitlines():
        plan['sources'][name] = sha(ROOT / name)
    plan['planned'] = dict(starts=2, chats=22, baseline=2, candidate=20,
        baseline_prefill=2048, candidate_prefill=512, context=16384,
        private_cache_minimum=8192, actual_ssd_writes=True,
        exact_cache_output=True, full_vs_chunk_logprobs='record_and_require_equal',
        actual_document_tool=True, performance_claim=False)
    plan['derived_requests'] = ['tool_follow appends actual call ID and actual document SHA256',
        'private_follow appends actual assistant answer and one fixed follow-up question']
    plan['private_only'] = private
    if private:
        plan['planned'] = dict(starts=1, chats=2, candidate_prefill=512,
            context=16384, private_cache_minimum=8192, actual_ssd_writes=True,
            performance_claim=False, passed_business_repeated=False)
    save(path, plan)


class Stage(text_cache.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_MEDIA_STATE_CACHE_BUSINESS'
    requests_per_minute = 32
    tokens_per_minute = 200000
    baseline_first = True

    def start(self):
        self.kind = ('baseline' if self.baseline_first and not self.counts['starts']
                     else 'candidate')
        encrypted.background.probe_ports((self.plan['port'], self.plan['operations_port']))
        save(self.out / 'client-policy.json', self.client_policy())
        baseline = self.plan['baseline']
        require(sha(baseline['binary']) == baseline['sha256'], 'accepted baseline drift')
        binary = baseline['binary'] if self.kind == 'baseline' else self.plan['binary']
        args = [binary, 'serve', '--model-dir', self.plan['model'], '--host', '127.0.0.1',
            '--port', str(self.plan['port']), '--operations-port', str(self.plan['operations_port']),
            '--mtp', '--allow-media', '--max-seq', '1', '--max-len', '16384',
            '--max-prefill', '2048' if self.kind == 'baseline' else '512',
            '--max-tokens', '256', '--max-inflight-requests', '8', '--max-queued-requests', '2',
            '--object-store-dir', str(self.out / (self.kind + '-objects')),
            '--client-policy-file', str(self.out / 'client-policy.json')]
        if self.kind == 'candidate':
            (self.out / 'private-cache').mkdir(mode=0o700)
            args += ['--seq-capacities', '16384', '--session-cache-dir',
                     str(self.out / 'private-cache')]
        save(self.out / (self.kind + '-command.json'), args)
        self.budget('starts')
        with (self.out / (self.kind + '-model.log')).open('xb', buffering=0) as log:
            self.model = subprocess.Popen(args, cwd=ROOT, stdout=log,
                stderr=subprocess.STDOUT, pass_fds=(self.lease,))
        guard = responses.base.lifecycle.guard
        self.model_identity = guard.processes()[self.model.pid]
        self.history[self.model.pid] = self.model_identity
        save(self.out / (self.kind + '-identity.json'), dict(pid=self.model.pid, **self.model_identity))
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
            raise TimeoutError('visual cache model readiness')
        for name in ('config', 'capabilities'):
            status, raw = self.http('/v1/' + name, label=self.kind + '-' + name)
            require(status == 200, 'missing live ' + name)
            save(self.out / (self.kind + '-' + name + '.json'), json.loads(raw))
        if self.kind == 'candidate':
            media = read(self.out / 'candidate-capabilities.json')['media']
            require(media['state_cache'] and media['chunked_prefill'] and not media['mtp'],
                    'visual state capability missing')

    def chat(self, label, body, hit=False, answer=True):
        self.budget('controls')
        status, raw = self.http('/v1/chat/preflight', dict(request=body,
            include_prompt=True, include_token_ids=True), label=label + '-preflight')
        require(status == 200, 'preflight failed: ' + label)
        prepared = json.loads(raw)
        require('prompt_cache_breakpoint' not in prepared['prompt'], 'cache metadata leaked into prompt')
        save(self.out / (label + '-preflight.json'), prepared)
        self.budget('chats')
        save(self.out / (label + '.request.json'), body)
        status, raw = self.http('/v1/chat/completions', body, label=label, timeout=180)
        require(status == 200, 'actual Chat failed: ' + label)
        value = (read_sse if body.get('stream') else read_json)(io.BytesIO(raw))
        save(self.out / (label + '.result.json'), asdict(value))
        require(value.finish_reason == ('stop' if answer else 'tool_calls'),
                'generation terminal: ' + label)
        if answer:
            if label.startswith('schema_'):
                require(json.loads(value.message['content']) == dict(visible_colors=True),
                        'actual structured visual answer')
            elif label != 'tool_follow':
                require(value.message['content'].strip() == 'YES', 'actual visual answer: ' + label)
        cached = value.usage.get('prompt_tokens_details', {}).get('cached_tokens', 0)
        if hit is not None:
            require((cached >= (8192 if label == 'private_follow' else 1024)) if hit
                    else cached == 0, 'actual hit/miss: ' + label)
        self.same_model()
        self.drained(label + '-drain')
        return value

    def business(self):
        rows = self.plan['requests']
        values = {label: self.chat(label, rows[label]) for label in ('base_image', 'base_video')}
        self.cleanup()
        require(read(self.out / 'cleanup.json')['model_exit'] == 0, 'baseline clean stop')
        save(self.out / 'baseline-cleanup.json', read(self.out / 'cleanup.json'))
        self.model = None
        self.start()
        ordered = ('image_uncached', 'image_write', 'image_hit', 'image_changed',
            'image_key', 'image_client', 'image_append', 'video_uncached', 'video_write',
            'video_hit', 'video_changed', 'video_timestamp', 'schema_write', 'schema_hit')
        for label in ordered:
            self.active_client = text_cache.OTHER_CLIENT if label == 'image_client' else encrypted.CLIENT
            values[label] = self.chat(label, rows[label], hit=label in (
                'image_hit', 'image_append', 'video_hit', 'schema_hit'))
        self.active_client = encrypted.CLIENT
        for kind in ('image', 'video'):
            a, b = values['base_' + kind], values[kind + '_uncached']
            prepared = [read(self.out / (name + '-preflight.json')) for name in
                        ('base_' + kind, kind + '_uncached', kind + '_write',
                         kind + '_hit', kind + '_changed')]
            require(all(v['prompt'] == prepared[0]['prompt'] and
                        v['token_ids'] == prepared[0]['token_ids'] for v in prepared),
                    'comparison must use identical expanded tokens: ' + kind)
            require(1024 < len(prepared[0]['token_ids']) < 2048,
                    'comparison must span candidate chunks and fit baseline prefill')
            comparison = dict(message_equal=a.message == b.message,
                logprobs_equal=a.logprobs == b.logprobs,
                baseline=a.logprobs, candidate=b.logprobs)
            save(self.out / (kind + '-full-vs-chunk.json'), comparison)
            require(comparison['message_equal'] and comparison['logprobs_equal'],
                    'full/chunk output differs; preserve evidence for numerical review: ' + kind)
        for prefix in ('image', 'video', 'schema'):
            a, b = values[prefix + '_write'], values[prefix + '_hit']
            require(a.message == b.message and a.logprobs == b.logprobs,
                    'same-boundary SSD restore changed output: ' + prefix)
        for label in ('responses_write', 'responses_hit'):
            value = self.sdk(label, 'create', rows[label])
            require(responses.text(value).strip() == 'YES', 'Responses visual answer')
            responses.check_usage(value)
            cached = value['usage']['input_tokens_details']['cached_tokens']
            require(cached >= 1024 if label.endswith('hit') else cached == 0,
                    'Responses visual cache usage')
        call = self.chat('tool_call', rows['tool_call'], answer=False)
        calls = call.message.get('tool_calls', [])
        require(len(calls) == 1 and calls[0]['function']['name'] == 'project_document' and
            json.loads(calls[0]['function']['arguments']) == dict(path=DOCUMENT), 'tool arguments')
        require(sha(ROOT / DOCUMENT) == self.plan['sources'][DOCUMENT], 'real document drift')
        source = dict(path=DOCUMENT, sha256=sha(ROOT / DOCUMENT), content=(ROOT / DOCUMENT).read_text())
        save(self.out / 'actual-tool-execution.json', source)
        follow = copy.deepcopy(rows['tool_follow'])
        follow['messages'] += [call.message, dict(role='tool', tool_call_id=calls[0]['id'],
            content=json.dumps(dict(path=DOCUMENT, sha256=source['sha256'])))]
        result = self.chat('tool_follow', follow, hit=None)
        check_tool_reply(result.message['content'], source['sha256'])
        self.private_business(rows)
        require(self.counts['chats'] == 22, 'exact external request count')
        log = (self.out / 'candidate-model.log').read_text()
        proof = {}
        for label in ordered:
            writes = re.findall(r'\[prefix-cache\] request=' +
                re.escape(rows[label]['request_id']) +
                r' event=write prefix=(\d+) ttl=(\d+) saved=(\d+) bytes=(\d+)', log)
            should_write = label not in ('image_uncached', 'video_uncached',
                'image_hit', 'image_append', 'video_hit', 'schema_hit')
            require(bool(writes) == should_write and all(
                int(p) >= 1024 and ttl == '1800' and saved == '1' and int(size) > 0
                for p, ttl, saved, size in writes), 'actual SSD write: ' + label)
            proof[label] = dict(writes=writes)
        for label in ('image_hit', 'image_append', 'video_hit', 'schema_hit', 'private_follow'):
            expected = 'session' if label == 'private_follow' else 'shared'
            matches = re.findall(r'\[session-cache\] request=' + re.escape(rows[label]['request_id']) +
                r' event=restore prefix=(\d+) ms=\S+ error=(\S+) source=(\w+) layout=(\w+)', log)
            require(len(matches) == 1 and int(matches[0][0]) >= 1024 and
                matches[0][1:] == ('none', expected, 'target'), 'actual state restore: ' + label)
            proof.setdefault(label, {})['restores'] = matches
        save(self.out / 'business-checks.json', dict(requests=22, starts=2,
            state_evidence=proof,
            actual_full_and_chunk_comparison=True, exact_cache_output=True,
            changed_media_miss=True, actual_tool=True, private_restore=True,
            no_performance_claim=True))

    def private_business(self, rows):
        private = self.chat('private_write', rows['private_write'])
        require(private.usage['prompt_tokens'] >= 8192, 'private snapshot minimum')
        follow = copy.deepcopy(rows['private_follow'])
        follow['messages'] += [private.message, dict(role='user',
            content='Repeat only the same YES or NO answer.')]
        value = self.chat('private_follow', follow, hit=True)
        log = (self.out / 'candidate-model.log').read_text()
        matches = re.findall(r'\[session-cache\] request=' +
            re.escape(rows['private_follow']['request_id']) +
            r' event=restore prefix=(\d+) ms=\S+ error=(\S+) source=(\w+) layout=(\w+)', log)
        require(matches == [(str(value.usage['prompt_tokens_details']['cached_tokens']),
            'none', 'session', 'target')], 'private restore usage has no actual state proof')
        save(self.out / 'private-business-checks.json', dict(requests=2,
            actual_private_restore=matches, actual_answers=True))


class PrivateStage(Stage):
    limits = PRIVATE_LIMITS
    request_templates = staticmethod(private_requests)
    baseline_first = False
    success_status = 'PASS_PRIVATE_MEDIA_STATE_CACHE_BUSINESS'

    def business(self):
        self.private_business(self.plan['requests'])
        require(self.counts['starts'] == 1 and self.counts['chats'] == 2,
                'unfinished private business count')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    p = modes.add_parser('prepare')
    p.add_argument('--binary', type=Path, required=True)
    p.add_argument('--baseline', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--port', type=int, default=18772)
    p.add_argument('--private-only', action='store_true')
    p = modes.add_parser('run')
    p.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'prepare':
        prepare(args)
    else:
        def deadline(_signum, _frame):
            raise TimeoutError('media state business deadline')
        signal.signal(signal.SIGALRM, deadline)
        plan = read(args.plan)
        stage = PrivateStage if plan.get('private_only') else Stage
        signal.setitimer(signal.ITIMER_REAL, stage.limits['seconds'] - 40)
        with responses.base.lifecycle.child_subreaper():
            stage(plan).run()
        signal.setitimer(signal.ITIMER_REAL, 0)


if __name__ == '__main__':
    main()

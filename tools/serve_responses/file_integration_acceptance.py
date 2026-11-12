"""File business and compatible rollback: 3 starts/7 generations/9 model steps.

One continuous strict GPU lease, <=1200 seconds; no automatic request replay.
Default MTP text, native PDF vision, actual CSV preparation/count/SSE, frozen
history and cross-version original state reads. No full model/performance run.
"""
import argparse
import base64
import copy
import hashlib
import http.client
import json
from pathlib import Path
import signal
import subprocess
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_responses import file_preparation_acceptance as files
from tools.serve_responses import input_tokens_acceptance as tokens
from tools.serve_service.package import read, require, save, sha

encrypted, foreground, background = files.encrypted, files.foreground, files.background
LIMITS = dict(starts=3, chats=7, controls=2000, seconds=1200)
MODEL_STEPS = 9
PDF = 'tests/fixtures/file_nested/page.pdf'
LARGE_BYTES = 17 * 1024**2
ROLLBACK_COMMIT = 'eee091b4634d95f855e8c5937bb0a4a3c500bcda'


def requests():
    source = files.requests()
    schema = foreground.base.closed(dict(value=dict(type='integer'),
        color=dict(type='string', enum=['red', 'green', 'blue', 'white'])))
    pdf = dict(model=foreground.MODEL, temperature=0, enable_thinking=False,
        store=True, max_completion_tokens=128,
        messages=[dict(role='user', content=[dict(type='file', file=dict(
            filename='page.pdf', file_data=base64.b64encode((ROOT/PDF).read_bytes()).decode())),
            dict(type='text', text='Read the PDF page. Return the printed number '
                'in value, and the page background color in color as lowercase English.')])],
        response_format=dict(type='json_schema', json_schema=dict(name='pdf_page',
            strict=True, schema=schema)))
    return dict(pdf=pdf, csv_stream=dict(source['initial'], stream=True,
        stream_options=dict(include_obfuscation=False)), follow=source['follow'],
        chat=source['chat'])


def pdf_answer(value):
    require(value['choices'][0]['finish_reason'] == 'stop', 'PDF answer incomplete')
    actual = json.loads(value['choices'][0]['message']['content'])
    require(actual == dict(value=42, color='green'), 'PDF text/actual page color differs')


def replay_chat_messages(page, expected):
    require(not page['has_more'] and len(page['data']) == 1, 'stored Chat message page')
    message = page['data'][0]
    require(message['role'] == 'user' and message['content_parts'] == expected['content'],
            'stored original PDF content changed')
    # The public stored-message projection has id/content_parts; Chat input
    # accepts role/content. Re-submit the exact original file bytes. This does
    # not claim that Chat implements Responses' previous-response semantics.
    return [dict(role=message['role'], content=copy.deepcopy(message['content_parts']))]


def preparation_usage(metadata, expected_steps):
    headers = {k.lower(): v for k, v in metadata['headers'].items()}
    names = ('input-tokens', 'output-tokens', 'model-steps')
    values = [headers.get('x-q4t-preparation-'+name) for name in names]
    if not expected_steps:
        require(all(v is None for v in values), 'frozen count executed preparation')
        return dict(input_tokens=0, output_tokens=0)
    require(all(type(v) is str and v.isascii() and v.isdigit() for v in values),
            'missing/invalid actual preparation usage headers')
    inputs, outputs, steps = map(int, values)
    require(steps == expected_steps and inputs > 0 and outputs > 0,
            'actual preparation step or token count differs')
    return dict(input_tokens=inputs, output_tokens=outputs)


def prepare(args):
    rollback = args.rollback_binary.resolve(strict=True)
    source = args.rollback_source.resolve(strict=True)
    revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'],
                                       text=True).strip()
    require(revision == ROLLBACK_COMMIT and not subprocess.check_output(
        ['git', '-C', str(source), 'status', '--porcelain']), 'rollback source identity')
    cache = rollback.parent/'CMakeCache.txt'
    require(('CMAKE_HOME_DIRECTORY:INTERNAL='+str(source)) in cache.read_text().splitlines(),
            'rollback binary build directory/source mismatch')
    background.prepare(args, rows=requests(), limits=LIMITS, scope=__doc__)
    path = args.output.resolve()/'plan.json'
    plan = read(path)
    for name in (PDF, 'tools/serve_responses/file_integration_acceptance.py',
                 'tools/serve_responses/test_file_integration_acceptance.py'):
        plan['sources'][name] = sha(ROOT/name)
    plan['rollback'] = dict(binary=str(rollback), sha256=sha(rollback),
        source_commit=revision, source_directory=str(source), cmake_cache_sha256=sha(cache),
        qualification='pending actual cross-version read/replay; not the old Uploads package')
    plan['planned'] = dict(starts=3, chats=7, model_steps=MODEL_STEPS,
        input_counts=2, preflights=1, large_conversation_creates=1)
    plan['large_state'] = dict(encoded_bytes=LARGE_BYTES, repeated_character='A',
        sha256=hashlib.sha256(b'A'*LARGE_BYTES).hexdigest(), model_execution=False)
    plan['mutations'] = ['isolated object directory and policy',
        'graceful owned MTP->media->prior-format-capable binary transitions; no retry',
        'same persistent directory and client owner across explicit rollback']
    plan['derived_requests'] = [
        'PDF original comes from frozen source fixture; number and color are real model answers',
        'count-new-CSV runs one real preparation; frozen count must run zero model steps',
        'fresh SSE must retain one uninterrupted created->completed sequence through preparation',
        'previous ID/messages/cursors come only from actual accepted resources',
        'large Conversation source is binary zero CPU transport/state data, never sent to model',
        'prior binary reads new private chunks and file snapshots before actual frozen generation',
        'RPM/TPM include count preparation and match actual usage separately per process']
    save(path, plan)


class Stage(encrypted.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_FILE_INTEGRATION_AND_COMPATIBLE_ROLLBACK'
    requests_per_minute = 12

    def client_policy(self):
        value = super().client_policy()
        value['clients'][0]['max_body_bytes'] = (
            16 if self.counts['starts'] >= 2 else 96) * 1024**2
        return value

    def extra_model_args(self):
        args = ['--object-max-client-mib', '256', '--object-max-total-mib', '512']
        if self.counts['starts'] >= 1:
            args += ['--no-mtp', '--allow-media']
        return args

    def start(self):
        original = self.plan
        if self.counts['starts'] == 2:
            fallback = original['rollback']
            require(sha(fallback['binary']) == fallback['sha256'], 'rollback binary drift')
            self.plan = dict(original, binary=fallback['binary'],
                             binary_sha256=fallback['sha256'])
        try:
            super().start()
        finally:
            self.plan = original

    def http(self, path, body=None, *, label=None, timeout=120):
        if body is None:
            self.budget('controls')
        headers = {} if path in ('/readyz', '/metrics', '/v1/config') else dict(encrypted.HEADERS)
        if body is not None:
            headers['Content-Type'] = 'application/json'
        connection = http.client.HTTPConnection('127.0.0.1', self.plan['port'], timeout=timeout)
        started = time.monotonic()
        try:
            raw_body = None if body is None else json.dumps(body, separators=(',', ':')).encode()
            connection.request('GET' if body is None else 'POST', path, raw_body, headers)
            response = connection.getresponse()
            raw = response.read(96*1024**2+1)
            require(len(raw) <= 96*1024**2, 'bounded business response size')
            if label:
                (self.out/(label+'.wire')).write_bytes(raw)
                save(self.out/(label+'.http.json'), dict(status=response.status,
                    headers=dict(response.getheaders()), seconds=time.monotonic()-started,
                    body_bytes=len(raw), body_sha256=hashlib.sha256(raw).hexdigest()))
            return response.status, raw
        finally:
            connection.close()

    def steps(self):
        return files.model_steps((self.out/f"model-{self.counts['starts']}.log").read_text())

    def control_post(self, label, path, body):
        self.budget('controls')
        status, raw = self.http(path, body, label=label)
        require(status == 200, label+' HTTP failure: '+raw[:512].decode(errors='replace'))
        self.same_model()
        return json.loads(raw)

    def chat(self, label, body, *, pdf=False):
        self.budget('chats')
        status, raw = self.http('/v1/chat/completions', body, label=label)
        require(status == 200, label+' Chat HTTP failure: '+raw[:512].decode(errors='replace'))
        value = json.loads(raw)
        if pdf:
            pdf_answer(value)
        else:
            require(value['choices'][0]['message']['content'].strip() == '42',
                    'ordinary same-model Chat answer differs')
        self.drained(label+'-drain')
        return value

    def usage(self, label, values=(), chats=(), preparation=None):
        metrics = self.drained(label)
        preparation = preparation or dict(input_tokens=0, output_tokens=0)
        inputs = sum(v['usage']['input_tokens'] for v in values)
        outputs = sum(v['usage']['output_tokens'] for v in values)
        inputs += sum(v['usage']['prompt_tokens'] for v in chats) + preparation['input_tokens']
        outputs += sum(v['usage']['completion_tokens'] for v in chats) + preparation['output_tokens']
        count = len(values) + len(chats)
        key = '{client="'+encrypted.CLIENT+'"}'
        require(metrics['q4t_requests_total'] == count and
                metrics['q4t_requests_success_total'] == count and
                metrics['q4t_requests_error_total'] == 0, 'generation request accounting')
        require(metrics['q4t_client_rate_admitted_total'+key] == count +
                bool(preparation['input_tokens']) and
                metrics['q4t_client_rate_reserved_tokens'+key] == 0 and
                metrics['q4t_client_rate_settled_tokens_total'+key] == inputs+outputs,
                'preparation/final answer RPM/TPM accounting differs')
        require(metrics['q4t_prompt_tokens_total'] == inputs and
                metrics['q4t_generation_tokens_total'] == outputs, 'actual execution token accounting')

    def transition(self, label):
        self.cleanup()
        receipt = read(self.out/'cleanup.json')
        save(self.out/(label+'-cleanup.json'), receipt)
        require(receipt['model_exit'] == 0, 'owned transition was not graceful')
        self.model = None
        self.start()

    def original_messages(self, label, response):
        status, raw = self.http('/v1/chat/completions/'+response['id']+'/messages?order=asc',
                                label=label)
        require(status == 200, 'stored original Chat messages unavailable')
        page = json.loads(raw)
        return replay_chat_messages(page, self.plan['requests']['pdf']['messages'][0])

    def large_state(self, identity=None, label='large-state'):
        if identity is None:
            value = self.control_post(label+'-create', '/v1/conversations', dict(items=[
                dict(role='user', content=[dict(type='input_file', filename='capacity.bin',
                                                file_data='A'*LARGE_BYTES)])]))
            identity = value['id']
        status, raw = self.http('/v1/conversations/'+identity+'/items', label=label+'-read')
        require(status == 200, 'large private application read failed')
        page = json.loads(raw)
        require(not page['has_more'] and len(page['data']) == 1, 'large original item page')
        original = page['data'][0]['content'][0]['file_data']
        require(len(original) == LARGE_BYTES and hashlib.sha256(original.encode()).hexdigest() ==
                self.plan['large_state']['sha256'], 'large original bytes changed across versions')
        return identity

    def business(self):
        rows = self.plan['requests']
        mtp = self.chat('default-mtp-chat', rows['chat'])
        require(read(self.out/'config-1.json')['runtime']['mtp_enabled'], 'default MTP changed')
        require(self.steps() == 1, 'unexpected default model steps')
        self.usage('mtp-usage', chats=[mtp])
        self.transition('default-mtp')
        require(not read(self.out/'config-2.json')['runtime']['mtp_enabled'], 'media profile MTP mismatch')
        preflight = self.control_post('pdf-preflight', '/v1/chat/preflight', dict(request=rows['pdf']))
        require(preflight['file_preparation']['pdf_pages_rasterized'] == 1 and
                preflight['file_preparation']['source_bytes'] == (ROOT/PDF).stat().st_size and
                preflight['media']['visual_tokens'] > 0 and self.steps() == 0,
                'PDF preflight did not include actual page')
        pdf = self.chat('pdf', rows['pdf'], pdf=True)
        require(pdf['usage']['prompt_tokens'] == preflight['prompt_tokens'], 'PDF preflight/inference mismatch')
        self.original_messages('original-pdf', pdf)
        count = self.control_post('new-sheet-count', '/v1/responses/input_tokens',
                                  tokens.count_request(rows['csv_stream']))
        tokens.count_value(count)
        prep = preparation_usage(read(self.out/'new-sheet-count.http.json'), 1)
        require(self.steps() == 2, 'new sheet count did not execute one preparation')
        fresh = self.sdk('fresh-csv-sse', 'create', rows['csv_stream'])
        files.check_answer(fresh, 42)
        events = read(self.out/'fresh-csv-sse.sdk.json')['chunks']
        require(events[0]['type'] == 'response.created' and
                events[-1]['type'] == 'response.completed' and
                sum(e['type'] == 'response.created' for e in events) == 1 and
                [e['sequence_number'] for e in events] == list(range(len(events))),
                'fresh preparation SSE lifecycle changed')
        require(self.steps() == 4, 'fresh CSV did not execute preparation and answer')
        follow = dict(rows['follow'], previous_response_id=fresh['id'])
        frozen = self.control_post('frozen-count', '/v1/responses/input_tokens',
                                   tokens.count_request(follow))
        preparation_usage(read(self.out/'frozen-count.http.json'), 0)
        require(self.steps() == 4, 'frozen history re-executed preparation')
        second = self.sdk('frozen-follow', 'create', follow)
        files.check_answer(second, 7)
        tokens.check_count_usage(tokens.count_value(frozen), second)
        ordinary = self.chat('same-media-model-chat', rows['chat'])
        require(self.steps() == 6, 'unexpected first media model steps')
        large = self.large_state()
        self.usage('media-usage', values=[fresh, second], chats=[pdf, ordinary], preparation=prep)
        self.transition('candidate-media')
        self.large_state(large, 'rollback-large-state')
        messages = self.original_messages('rollback-original-pdf', pdf)
        stored = self.sdk('rollback-stored-response', 'retrieve', response_id=fresh['id'])
        files.check_answer(stored, 42)
        require(self.steps() == 0, 'state read started model work')
        final = self.sdk('rollback-frozen-follow', 'create', follow)
        files.check_answer(final, 7)
        replay = self.chat('rollback-pdf', dict(rows['pdf'], messages=messages), pdf=True)
        require(self.steps() == 2, 'rollback re-executed frozen preparation')
        self.usage('rollback-usage', values=[final], chats=[replay])
        save(self.out/'business-checks.json', dict(starts=3, generations=7,
            model_steps=9, original_pdf_sha256=self.plan['sources'][PDF],
            original_csv_sha256=hashlib.sha256(files.CSV).hexdigest(),
            default_mtp=True, native_pdf_vision=True, preflight_token_parity=True,
            fresh_preparation_sse=True, count_preparation_usage=prep,
            frozen_count_and_generation=True, large_cross_version_state=True,
            rollback=self.plan['rollback'], exact_rpm_tpm=True,
            production_state_unchanged=True, performance_claim=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    p = modes.add_parser('prepare')
    p.add_argument('--binary', type=Path, required=True)
    p.add_argument('--rollback-binary', type=Path, required=True)
    p.add_argument('--rollback-source', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--port', type=int, default=19041)
    p = modes.add_parser('run')
    p.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'prepare':
        prepare(args)
    else:
        def timeout(_signal, _frame):
            raise TimeoutError('file integration deadline')
        prior = signal.signal(signal.SIGALRM, timeout)
        try:
            signal.setitimer(signal.ITIMER_REAL, LIMITS['seconds']-40)
            with foreground.base.lifecycle.child_subreaper():
                Stage(read(args.plan)).run()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, prior)


if __name__ == '__main__':
    main()

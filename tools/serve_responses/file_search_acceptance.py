"""Native project retrieval: one primary start, six model-bearing requests, 900s.

Actual source upload/index/search, SDK JSON/SSE/citations, background/history,
shared RPM/TPM and owner isolation. No full performance or quality matrix.
"""
import argparse
import copy
import http.client
import json
from pathlib import Path
import signal
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_responses import background_acceptance as background
from tools.serve_responses import encryption_acceptance as encrypted
from tools.serve_responses.test_contract import errors
from tools.serve_service.package import read, require, save, sha

foreground = encrypted.foreground
LIMITS = dict(starts=1, chats=6, controls=2000, seconds=900)
OTHER = 'file-search-other'
DOCUMENTS = [('docs/SSD_OFFLOAD_CONTRACT.md', 1, 28),
             ('docs/EVALUATION.md', 1, 17)]


def documents():
    return [dict(path=path, first_line=first, last_line=last,
                 text='\n'.join((ROOT/path).read_text().splitlines()[first-1:last])+'\n',
                 sha256=sha(ROOT/path)) for path, first, last in DOCUMENTS]


def requests():
    base = dict(model=foreground.MODEL, temperature=0, store=True,
                reasoning=dict(effort='none'), max_output_tokens=384,
                max_tool_calls=1, parallel_tool_calls=False,
                tools=[dict(type='file_search', vector_store_ids=['vs_actual'],
                            max_num_results=2)],
                tool_choice=dict(type='file_search'),
                instructions='Use file_search with exactly one query. Answer only '
                'from its actual returned sources in at most two sentences. Copy '
                'the exact citation_marker of the supporting source into the answer. '
                'Do not invent file IDs or claim any other actions.')
    question = ('According to this project, where may MoE expert capacity be '
                'offloaded, and why is moving it to CPU memory insufficient? '
                'Use the terms SSD and DRAM in your answer.')
    structured = dict(base, input='For a non-performance service bug fix, what '
        'validation does this project require? Return a JSON object with an '
        'answer string, including the exact supporting citation_marker.',
        stream=True, stream_options=dict(include_obfuscation=False),
        text=dict(format=dict(type='json_schema', name='project_answer', strict=True,
            schema=foreground.base.closed(dict(answer=dict(type='string', minLength=1))))))
    return dict(search=dict(query='Thor MoE expert capacity SSD CPU DRAM',
                            max_num_results=2),
        included=dict(base, input=question, include=['file_search_call.results']),
        hidden=structured,
        background=dict(base, input=question, background=True,
                        include=['file_search_call.results']),
        history=dict(model=foreground.MODEL, temperature=0, store=True,
            reasoning=dict(effort='none'), max_output_tokens=128,
            input='Using the previous retrieved source, repeat the permitted '
                  'capacity offload target and memory type. Include SSD and DRAM.'),
        chat=dict(model=foreground.MODEL, temperature=0, enable_thinking=False,
                  max_completion_tokens=32,
                  messages=[dict(role='user', content='Compute 17 + 25. Reply only with the decimal number.')]))


def check_answer(response, expected_file, *, included, structured=False,
                 expected_terms=('SSD', 'DRAM')):
    require(not errors(response, 'response'), 'Responses schema mismatch')
    require(response['status'] == 'completed' and response['error'] is None,
            'native search answer did not complete')
    calls = [item for item in response['output'] if item['type'] == 'file_search_call']
    require(len(calls) == 1 and calls[0]['status'] == 'completed' and
            len(calls[0]['queries']) == 1, 'expected one actual search/query')
    result = calls[0]['results']
    if included:
        require(result and any(hit['file_id'] == expected_file for hit in result),
                'expected actual project source absent')
    else:
        require(result is None, 'private search results escaped include projection')
    parts = [part for item in response['output'] if item['type'] == 'message'
             for part in item['content'] if part['type'] == 'output_text']
    require(parts, 'native search returned no answer')
    text = ''.join(part['text'] for part in parts)
    citations = [citation for part in parts for citation in part['annotations']
                 if citation['type'] == 'file_citation']
    require(any(c['file_id'] == expected_file for c in citations),
            'answer omitted actual source citation')
    for part in parts:
        for citation in part['annotations']:
            if citation['type'] != 'file_citation':
                continue
            marker = '[file:'+citation['file_id']+']'
            require(part['text'][:citation['index']].endswith(marker),
                    'source citation character position mismatch')
    if structured:
        answer = json.loads(text)
        require(set(answer) == {'answer'} and isinstance(answer['answer'], str),
                'structured answer differs from original schema')
        require(any(term in answer['answer'].lower() for term in
                    ('targeted', 'specific', 'affected', '针对', '直接受影响')),
                'project validation answer did not use actual policy')
    else:
        require(all(term in text for term in expected_terms),
                'project capacity answer differs')
    return dict(text=text, queries=calls[0]['queries'], citations=citations,
                usage=response['usage'])


def check_meter(before, after, response):
    suffix = '{client="'+encrypted.CLIENT+'"}'
    delta = lambda key: after.get(key, 0)-before.get(key, 0)
    require(delta('q4t_client_rate_admitted_total'+suffix) == 1,
            'native tool continuation reserved another RPM')
    require(after['q4t_client_rate_reserved_tokens'+suffix] == 0,
            'native query/continuation reservation retained')
    usage = response['usage']
    require(delta('q4t_prompt_tokens_total') == usage['input_tokens'] and
            delta('q4t_generation_tokens_total') == usage['output_tokens'] and
            delta('q4t_client_rate_settled_tokens_total'+suffix) == usage['total_tokens'],
            'native tool query/primary actual usage mismatch')


def check_backend_health(config):
    health = config['native_embedding_health']
    # Index preparation loads metadata first; the first embedding later loads
    # weights. Both count as attempts, while weights are uploaded only once.
    require(health['state'] == 'ready' and health['reason'] == 'none' and
            health['load_attempts'] == 2 and health['load_failures'] == 0 and
            health['device_bytes'] == 1191553024 and
            not health['core_readiness_dependency'],
            'native backend lifecycle differs')
    for section in ('vector_ingestion', 'responses_background'):
        jobs = config[section]
        require(jobs['state'] == 'ready' and jobs['owned'] == 0 and
                jobs.get('active', jobs.get('running')) == 0 and
                jobs.get('errors', jobs.get('recovery_errors')) == 0,
                'native application work did not settle: '+section)
    return health


def reconcile(directory, output):
    """Validate immutable original evidence after the final oracle-only fix."""
    directory, output = Path(directory).resolve(), Path(output).resolve()
    require(directory.is_relative_to(ROOT/'.q4t-work') and
            output.is_relative_to(ROOT/'.q4t-work') and not output.exists(),
            'reconciliation must preserve original evidence')
    plan, result = read(directory/'plan.json'), read(directory/'result.json')
    require(result['status'] == 'FAILED' and
            result['error'].endswith('RuntimeError: native backend lifecycle differs\n') and
            result['counts']['starts'] == 1 and result['counts']['chats'] == 6 and
            result['seconds'] < LIMITS['seconds'], 'different failure or budget')
    lease = read(directory/'lease/exit.json')
    require(lease['status'] == 'COMMAND_FAILED' and lease['child_returncode'] == 1 and
            lease['compute_backend'] == 'nvml' and lease['lease_inherited_by_child'],
            'original strict lease not valid')
    cleanup = read(directory/'cleanup.json')
    require(not cleanup['remaining'] and cleanup['model_exit'] == 0 and
            cleanup['ports_released'] and cleanup['lease_continuously_held'],
            'original cleanup incomplete')
    # Runtime/source/asset identity cannot change to make a failed run pass.
    # Only this acceptance driver and its direct CPU oracle may differ.
    changed = []
    for name, digest in plan['sources'].items():
        if sha(ROOT/name) != digest:
            changed.append(name)
    require(set(changed) <= {
        'tools/serve_responses/file_search_acceptance.py',
        'tools/serve_responses/test_file_search_acceptance.py'}, 'runtime source drift')
    require(sha(plan['binary']) == plan['binary_sha256'] == result['binary_sha256'],
            'business executable changed')
    for name, digest in plan['protected'].items():
        require(sha(name) == digest, 'default changed')
    for name, digest in plan['embedding_assets_sha256'].items():
        require(sha(Path(plan['embedding_assets'])/name) == digest, 'embedding asset drift')
    ids = read(directory/'actual-ids.json')
    files = ids['files']
    for index, document in enumerate(plan['documents']):
        require(sha(ROOT/document['path']) == document['sha256'], 'document source drift')
        content = read(directory/f'indexed-source-{index}.wire')
        require([row['text'] for row in content['data']] == [document['text']],
                'original indexed content differs')
    search = read(directory/'standalone-search.wire')
    require(search['data'][0]['file_id'] == files[0] and
            read(directory/'search-other-owner.http.json')['status'] == 404,
            'original search/routing differs')
    before_denied = read(directory/'search-after.metrics.json')[-1]
    after_denied = read(directory/'search-other-owner-after.metrics.json')[-1]
    require(before_denied['q4t_prompt_tokens_total'] == after_denied['q4t_prompt_tokens_total'],
            'denied query used the model')
    answers = {}
    for name in ('included', 'hidden', 'background'):
        sdk = read(directory/(name+'.sdk.json'))
        require(sdk['ok'] and len(sdk['exchanges']) == 1, 'original SDK did not complete')
        value = sdk['result']
        if name == 'background':
            polls = sorted(directory.glob('background-poll-*.wire'),
                           key=lambda p:int(p.stem.split('-')[-1]))
            require(polls, 'original background observation absent')
            value = read(polls[-1])
            require(value['id'] == sdk['result']['id'], 'background identity changed')
        else:
            check_meter(read(directory/(name+'-before.metrics.json'))[-1],
                        read(directory/(name+'-after.metrics.json'))[-1], value)
        answers[name] = check_answer(value, files[1] if name == 'hidden' else files[0],
                                     included=name != 'hidden', structured=name == 'hidden')
        for event in sdk.get('chunks') or []:
            require(not errors(event, 'response_event'), 'original event schema mismatch')
        retained = read(directory/(name+'-stored.sdk.json'))
        require(retained['ok'] and retained['result']['id'] == value['id'] and
                any(item.get('results') for item in retained['result']['output']
                    if item['type'] == 'file_search_call'), 'original retained evidence missing')
    follow = read(directory/'history.sdk.json')['result']
    text = foreground.text(follow)
    require(follow['status'] == 'completed' and 'SSD' in text and 'DRAM' in text and
            not any(item['type'] == 'file_search_call' for item in follow['output']),
            'original history differs')
    chat = read(directory/'healthy-chat.wire')['choices'][0]
    require(chat['finish_reason'] == 'stop' and chat['message']['content'].strip() == '42',
            'original ordinary Chat failed')
    final = read(directory/'final.metrics.json')[-1]
    require(final['q4t_execution_steps_total'] <= 6000 and final['q4t_active_chats'] == 0,
            'original work bound/cleanup differs')
    health = check_backend_health(read(directory/'final-config.wire'))
    artifacts = [p for p in directory.iterdir() if p.is_file()]
    artifacts += [p for p in (directory/'lease').iterdir() if p.is_file()]
    save(output, dict(status='ACCEPTED_ORIGINAL_BUSINESS_AFTER_ORACLE_FIX',
        original_status=result['status'], original_lease_status=lease['status'],
        counts=result['counts'], seconds=result['seconds'], binary_sha256=result['binary_sha256'],
        original=str(directory), original_plan_sha256=sha(directory/'plan.json'),
        changed_oracles=changed, actual_answers=answers, backend_health=health,
        gpu_starts_added=0, repeated_model_requests=0, default_unchanged=True,
        artifacts_sha256={str(p.relative_to(directory)):sha(p) for p in artifacts}))


def prepare(args):
    background.prepare(args, rows=requests(), limits=LIMITS, scope=__doc__)
    path = args.output.resolve()/'plan.json'
    plan = read(path)
    assets = args.assets.resolve(strict=True)
    plan['embedding_assets'] = str(assets)
    plan['embedding_assets_sha256'] = {name:sha(assets/name) for name in
        ['config.json', 'tokenizer.json', 'model.safetensors']}
    require(plan['embedding_assets_sha256']['model.safetensors'] ==
            '0437e45c94563b09e13cb7a64478fc406947a93cb34a7e05870fc8dcd48e23fd',
            'accepted embedding weights changed')
    plan['documents'] = documents()
    for row in plan['documents']:
        plan['sources'][row['path']] = row['sha256']
    plan['planned'] = dict(primary_starts=1, native_embedding_weight_uploads=1,
        http_model_requests=6, responses_primary_steps_max=8,
        embedding_inputs_max=51, native_execution_steps_max=6000,
        source_documents=2, chunks_per_document_max=1)
    plan['mutations'] = ['only own isolated Files/vector store/Responses objects',
        'normal explicit shutdown of own test process; no running service changes']
    plan['derived_requests'] = ['replace vs_actual with actual scoped vector-store ID',
        'upload exact pinned repository excerpts; never synthesize source text',
        'history/retrieve/replay use original IDs without repeating generation',
        'one continuous strict GPU lease, no automatic retries or default changes']
    save(path, plan)


class Stage(encrypted.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_NATIVE_FILE_SEARCH_BUSINESS'
    requests_per_minute = 512
    tokens_per_minute = 262144

    def client_policy(self):
        policy = super().client_policy()
        policy['clients'][0]['max_prompt_tokens'] = 32768
        policy['clients'].append(dict(policy['clients'][0], id=OTHER))
        return policy

    def extra_model_args(self):
        return ['--embedding-model-dir', self.plan['embedding_assets'],
                '--object-max-client-mib', '256', '--object-max-total-mib', '512']

    def start(self):
        require(self.plan['documents'] == documents(), 'project source drift')
        for name, digest in self.plan['embedding_assets_sha256'].items():
            require(sha(Path(self.plan['embedding_assets'])/name) == digest,
                    'embedding asset drift')
        super().start()

    def api(self, label, method, path, body=None, *, content_type='application/json',
            owner=encrypted.CLIENT, status=200, schema=None):
        self.budget('controls')
        headers = dict(encrypted.HEADERS, **{'X-Q4T-Client-Id':owner})
        if body is not None:
            headers['Content-Type'] = content_type
        if isinstance(body, dict):
            body = json.dumps(body, ensure_ascii=False).encode()
        save(self.out/(label+'.request.json'), dict(method=method, path=path,
            body=body.decode() if body is not None else None, owner=owner))
        connection = http.client.HTTPConnection('127.0.0.1', self.plan['port'], timeout=120)
        try:
            connection.request(method, path, body, headers)
            reply = connection.getresponse()
            raw = reply.read(4*1024*1024+1)
            (self.out/(label+'.wire')).write_bytes(raw)
            save(self.out/(label+'.http.json'), dict(status=reply.status,
                headers=dict(reply.getheaders())))
            require(len(raw) <= 4*1024*1024 and reply.status == status,
                    label+': '+raw[:512].decode(errors='replace'))
            value = json.loads(raw)
            if schema:
                require(not errors(value, schema), label+' schema')
            self.same_model()
            return value
        finally:
            connection.close()

    def ingest(self):
        files = []
        for index, source in enumerate(self.plan['documents']):
            wire = ('--q4t\r\nContent-Disposition: form-data; name="purpose"\r\n\r\nuser_data'
                '\r\n--q4t\r\nContent-Disposition: form-data; name="file"; filename="'+
                Path(source['path']).name+'"\r\nContent-Type: text/markdown\r\n\r\n'+
                source['text']+'\r\n--q4t--\r\n').encode()
            files.append(self.api(f'upload-{index}', 'POST', '/v1/files', wire,
                content_type='multipart/form-data; boundary=q4t', schema='file_resource')['id'])
        store = self.api('store-create', 'POST', '/v1/vector_stores',
            dict(name='Real project source records', file_ids=files,
                 chunking_strategy=dict(type='static', static=dict(
                     max_chunk_size_tokens=4096, chunk_overlap_tokens=0))),
            schema='vector_store_resource')['id']
        for index, file in enumerate(files):
            for attempt in range(240):
                item = self.api(f'index-{index}-{attempt}', 'GET',
                    f'/v1/vector_stores/{store}/files/{file}', schema='vector_file_resource')
                if item['status'] == 'completed':
                    break
                require(item['status'] == 'in_progress', 'actual document indexing failed')
                time.sleep(.25)
            else:
                raise TimeoutError('document indexing did not complete')
            content = self.api(f'indexed-source-{index}', 'GET',
                f'/v1/vector_stores/{store}/files/{file}/content', schema='vector_file_content')
            require(len(content['data']) == 1 and
                    content['data'][0]['text'] == self.plan['documents'][index]['text'],
                    'indexed text differs or exceeded frozen one-chunk budget')
        return store, files

    def business(self):
        store, files = self.ingest()
        save(self.out/'actual-ids.json', dict(vector_store=store, files=files))
        before = self.drained('search-before')
        self.budget('chats')
        search = self.api('standalone-search', 'POST', f'/v1/vector_stores/{store}/search',
                          self.plan['requests']['search'], schema='vector_search_page')
        require(search['data'] and search['data'][0]['file_id'] == files[0],
                'actual embedding search ranked wrong project source')
        after = self.drained('search-after')
        require(after['q4t_prompt_tokens_total'] > before['q4t_prompt_tokens_total'],
                'search did not consume actual query model tokens')
        before_denied = after
        self.api('search-other-owner', 'POST', f'/v1/vector_stores/{store}/search',
                 self.plan['requests']['search'], owner=OTHER, status=404)
        after_denied = self.drained('search-other-owner-after')
        require(after_denied['q4t_prompt_tokens_total'] == before_denied['q4t_prompt_tokens_total'],
                'cross-owner search reached the embedding model')

        results = {}
        for name in ('included', 'hidden', 'background'):
            request = copy.deepcopy(self.plan['requests'][name])
            request['tools'][0]['vector_store_ids'] = [store]
            before = self.drained(name+'-before')
            value = self.sdk(name, 'create', request, drain=name != 'background')
            if name == 'background':
                response_id = value['id']
                for attempt in range(240):
                    if value['status'] not in ('queued', 'in_progress'):
                        break
                    time.sleep(.25)
                    value = self.api(f'background-poll-{attempt}', 'GET',
                        '/v1/responses/'+response_id+'?include[]=file_search_call.results',
                        schema='response')
                else:
                    raise TimeoutError('background search did not finish')
            after = self.drained(name+'-after')
            if name != 'background':
                check_meter(before, after, value)
            expected = files[1] if name == 'hidden' else files[0]
            results[name] = check_answer(value, expected, included=name != 'hidden',
                                         structured=name == 'hidden')
            results[name]['id'] = value['id']
            # Retrieve with include recovers the actual retained evidence even
            # when create hid it; no second search or model execution.
            retained = self.sdk(name+'-stored', 'retrieve', response_id=value['id'],
                query=dict(include=['file_search_call.results']))
            calls = [item for item in retained['output'] if item['type'] == 'file_search_call']
            require(calls and calls[0]['results'], 'stored actual search evidence absent')

        history = dict(self.plan['requests']['history'],
                       previous_response_id=results['included']['id'])
        follow = self.sdk('history', 'create', history)
        text = foreground.text(follow)
        require(follow['status'] == 'completed' and 'SSD' in text and 'DRAM' in text and
                not any(item['type'] == 'file_search_call' for item in follow['output']),
                'retained search history was lost or reexecuted')
        self.budget('chats')
        chat = self.api('healthy-chat', 'POST', '/v1/chat/completions',
                        self.plan['requests']['chat'], schema='chat_completion')
        require(chat['choices'][0]['finish_reason'] == 'stop' and
                chat['choices'][0]['message']['content'].strip() == '42',
                'ordinary core Chat failed after native retrieval')
        last = self.drained('final')
        require(last['q4t_execution_steps_total'] <= 6000, 'frozen work budget exceeded')
        status, raw = self.http('/v1/config', label='final-config')
        require(status == 200, 'final configuration unavailable')
        config = json.loads(raw)
        health = check_backend_health(config)
        save(self.out/'business.json', dict(status='PASS', results=results,
            history_id=follow['id'], source_documents=self.plan['documents'],
            final_metrics=last, native_embedding=health))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    command = modes.add_parser('prepare')
    command.add_argument('--binary', type=Path, required=True)
    command.add_argument('--assets', type=Path, required=True)
    command.add_argument('--output', type=Path, required=True)
    command.add_argument('--port', type=int, default=19091)
    command = modes.add_parser('run')
    command.add_argument('--plan', type=Path, required=True)
    command = modes.add_parser('reconcile')
    command.add_argument('--original', type=Path, required=True)
    command.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'prepare':
        prepare(args)
    elif args.mode == 'reconcile':
        reconcile(args.original, args.output)
    else:
        def deadline(*_):
            raise TimeoutError('bounded file search business deadline')
        signal.signal(signal.SIGALRM, deadline)
        signal.setitimer(signal.ITIMER_REAL, LIMITS['seconds']-40)
        try:
            with foreground.base.lifecycle.child_subreaper():
                Stage(read(args.plan)).run()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)


if __name__ == '__main__':
    main()

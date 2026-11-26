"""Exercise a Responses cache policy with an actual strict OpenAI Python client."""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import sys

import openai


def exercise(plan):
    output = Path(plan['capture'])
    output.mkdir(mode=0o700)
    retention = plan['expected']
    with ExitStack() as stack:
        def connect(key):
            http = openai.DefaultHttpxClient(verify=plan['certificate'], trust_env=False)
            return stack.enter_context(openai.OpenAI(
                base_url=plan['base'], api_key=key, http_client=http, max_retries=0,
                _strict_response_validation=True, timeout=60))

        owner, peer = connect(plan['key']), connect(plan['other'])
        emitted = []

        def consume(stream):
            events = []
            with stream:
                for event in stream:
                    assert event.sequence_number == len(events)
                    data = event.model_dump(mode='json', by_alias=True, exclude_unset=True)
                    if 'response' in data:
                        assert data['response']['prompt_cache_retention'] == retention
                    events.append(data)
            assert events[0]['type'] == 'response.created'
            assert events[-1]['type'] == 'response.completed'
            return events

        if plan['operation'] == 'create':
            request = dict(model=plan['model'], input=plan.get('prompt', 'Reply only with the integer product of 6 and 7.'),
                           metadata={'prompt_cache_retention': 'in_memory'}, temperature=0,
                           reasoning={'effort': 'none'}, store=True, max_output_tokens=32)
            if 'requested' in plan:
                request['prompt_cache_retention'] = plan['requested']
            if plan['stream']:
                emitted = consume(owner.responses.create(**request, stream=True,
                                                         stream_options={'include_obfuscation': False}))
                rid = emitted[-1]['response']['id']
            else:
                result = owner.responses.create(**request)
                assert result.prompt_cache_retention == retention
                rid = result.id
        else:
            assert plan['operation'] == 'read'
            rid = plan['id']

        raw = owner.responses.with_raw_response.retrieve(rid)
        vary = {value.strip().lower() for value in raw.headers.get('vary', '').split(',')}
        assert {'x-stainless-lang', 'x-stainless-package-version'} <= vary
        result = raw.parse()
        body = raw.http_response.content
        saved = json.loads(body)
        assert result.id == rid and result.status == 'completed'
        assert result.prompt_cache_retention == saved['prompt_cache_retention'] == retention
        if 'text' in plan:
            assert result.output_text.strip() == plan['text']
        if plan.get('check_metadata'):
            assert result.metadata['prompt_cache_retention'] == 'in_memory'
        replay = consume(owner.responses.retrieve(rid, stream=True, include_obfuscation=False))
        assert replay[-1]['response']['id'] == rid
        assert replay[-1]['response']['output'] == saved['output']
        assert replay[-1]['response']['prompt_cache_options'] == saved['prompt_cache_options']
        if emitted:
            assert emitted == replay
        try:
            peer.responses.retrieve(rid)
        except openai.NotFoundError as error:
            assert error.status_code == 404
        else:
            raise AssertionError('Responses history was visible to another customer')
        (output/'resource.json').write_bytes(body)
        (output/'replay.json').write_text(json.dumps(replay, ensure_ascii=False))
        if emitted:
            (output/'create.json').write_text(json.dumps(emitted, ensure_ascii=False))
        return dict(sdk=openai.__version__, id=rid, retention=retention,
                    cache_options=saved['prompt_cache_options'], get_sha256=hashlib.sha256(body).hexdigest(),
                    events=len(replay), emitted=len(emitted), foreign=404)


if __name__ == '__main__':
    print(json.dumps(exercise(json.load(sys.stdin)), ensure_ascii=False))

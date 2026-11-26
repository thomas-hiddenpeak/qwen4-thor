"""Check installed OpenAI Python resource methods with an offline HTTP transport."""
import inspect
import json
from pathlib import Path
import re
import warnings

import openai

try:
    import httpx2 as httpx
except ImportError:
    import httpx


CATALOG = Path(__file__).resolve().parents[2] / 'docs/evidence/serve-sdk-routes-20261126/methods.json'
ID = 'q4tfixture'


def fixture(name):
    if name in ('file', 'image', 'video', 'audio', 'consent_recording',
                'reference_audio', 'sample', 'image_reference'):
        return ('fixture.bin', b'fixture', 'application/octet-stream')
    if name.endswith('_ids') or name in (
            'messages', 'tools', 'resources', 'instructions_list', 'events', 'chunks',
            'items', 'turns', 'skills', 'testing_criteria', 'tool_outputs', 'parts',
            'part_ids', 'entries'):
        return []
    if name in ('data_source', 'data_source_config', 'grading_criteria', 'session',
                'environment', 'workflow', 'source', 'config', 'content', 'scope',
                'completion_window', 'action', 'metadata', 'reasoning', 'strategy',
                'response_format'):
        return {}
    if name in ('limit', 'max_tokens', 'bytes', 'size', 'expires_after', 'seconds',
                'index', 'part_number'):
        return 1
    return ID


def arguments(fn, verb, path):
    positional = []
    kwargs = {}
    signature = inspect.signature(fn)
    for name, param in signature.parameters.items():
        if param.default != inspect.Parameter.empty or param.kind in (
                param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        if param.kind == param.POSITIONAL_ONLY:
            positional.append(fixture(name))
        else:
            kwargs[name] = fixture(name)
    if verb == 'POST' and path in ('/responses', '/chat/completions', '/completions', '/embeddings'):
        for key in ('model', 'input', 'messages', 'prompt'):
            if key in signature.parameters:
                kwargs.setdefault(key, fixture(key))
    if verb == 'POST' and path == '/agents/automations':
        kwargs.update(type='one_time', run_at='2026-12-01T00:00:00Z', name='fixture', target={})
    if verb == 'POST' and path == '/agents/environments/{environment_id}/files':
        kwargs.update(file_id=ID, path='fixture.txt', type='file_id')
    if verb == 'POST' and path == '/audio/voices':
        kwargs.update(name='fixture', prompt='fixture', type='voice_design')
    if verb == 'POST' and path == '/audio/voice_consents':
        kwargs['recording'] = fixture('file')
    if verb == 'POST' and path == '/uploads/{upload_id}/parts':
        kwargs['data'] = b'fixture'
    if path == '/embeddings' and 'encoding_format' in signature.parameters:
        kwargs['encoding_format'] = 'float'
    if path.endswith('/content') and verb in ('PUT', 'POST') and 'file' in signature.parameters:
        kwargs['file'] = fixture('file')
    return positional, kwargs


def main():
    catalog = json.loads(CATALOG.read_bytes())
    version = openai.__version__
    if version not in catalog['versions']['python']:
        raise ValueError(f'No frozen expectation for OpenAI Python {version}')
    seen = []

    def intercept(request):
        if request.url.host != 'sdk.invalid':
            raise ValueError('unexpected mock destination')
        selectors = request.url.params.get_list('beta')
        path = request.url.path
        if selectors:
            path += '?' + '&'.join('beta=' + value for value in selectors)
        seen.append((request.method, path))
        return httpx.Response(418, request=request, json={'error': {'message': 'offline probe'}})

    counts = dict(canonical=0, absent=0, legacy_cancel=0)
    transport = httpx.Client(transport=httpx.MockTransport(intercept), trust_env=False)
    with openai.OpenAI(api_key='fixture', base_url='https://sdk.invalid/v1',
                       max_retries=0, http_client=transport) as client:
        for verb, path, accessor, _, old_status in catalog['rows']:
            expected = old_status if version == '2.8.1' else 'canonical'
            seen.clear()
            method = client
            try:
                for name in accessor.split('.'):
                    method = getattr(method, name)
            except AttributeError:
                if expected != 'absent':
                    raise
                counts['absent'] += 1
                continue
            positional, kwargs = arguments(method, verb, path)
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', DeprecationWarning)
                try:
                    method(*positional, **kwargs)
                except openai.APIStatusError as error:
                    if error.status_code != 418:
                        raise
            target = catalog['legacy_cancel_path'] if expected == 'legacy_cancel' else path
            wanted = (verb, '/v1' + re.sub(r'\{[^}]+\}', ID, target))
            if expected == 'absent' or seen != [wanted]:
                raise ValueError(f'{accessor} saw {seen}, expected {expected} {wanted}')
            counts[expected] += 1
    print(json.dumps(dict(version=version, observed=counts, methods=len(catalog['rows']))))


if __name__ == '__main__':
    main()

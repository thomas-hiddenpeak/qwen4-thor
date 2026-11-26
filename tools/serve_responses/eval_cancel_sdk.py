"""Exercise typed Evals cancellation and project isolation against a native TLS endpoint."""
from hashlib import sha256
import json
from pathlib import Path
import ssl
import sys
import time
from urllib.parse import urlsplit

import openai

try:
    import httpx2 as httpx
except ImportError:
    import httpx


def expect(code, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except openai.APIStatusError as error:
        if error.status_code == code:
            return
        raise
    raise AssertionError(f'Expected HTTP {code}')


def resource_digest(response, eid, rid):
    value = response.http_response.json()
    url = urlsplit(value['report_url'])
    expected = f'/v1/evals/{eid}/runs/{rid}/report'
    assert url.path == expected and not url.query and not url.fragment
    assert value['object'] == 'eval.run' and value['eval_id'] == eid and value['id'] == rid
    assert value['status'] == 'canceled' and value['data_source']['type'] == 'responses'
    # The public report URL follows the active gateway origin.
    canonical = dict(value, report_url=url.path)
    return sha256(json.dumps(canonical, separators=(',', ':'), sort_keys=True).encode()).hexdigest()


def main():
    plan = json.load(sys.stdin)
    legacy = openai.__version__ == '2.8.1'
    if legacy != ('eval_id' not in plan):
        raise ValueError('Use the legacy SDK first, then the current SDK with its saved IDs')
    ctx = ssl.create_default_context(cafile=plan.get('certificate'))
    next_request = 0.0

    def pace(_request):
        nonlocal next_request
        delay = next_request - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        next_request = time.monotonic() + plan.get('request_interval', 0)

    def client(key):
        return openai.OpenAI(base_url=plan['base'], api_key=key, max_retries=0,
                             http_client=httpx.Client(verify=ctx, trust_env=False, timeout=45,
                                                     event_hooks={'request': [pace]}))
    with client(plan['key']) as owner, client(plan['other']) as peer:
        if legacy:
            definition = owner.evals.create(
                name='Historical SDK cancellation', data_source_config={
                    'type': 'custom', 'item_schema': {'type': 'object',
                    'properties': {'prompt': {'type': 'string'}}, 'required': ['prompt']}},
                testing_criteria=[{'type': 'string_check', 'name': 'Arithmetic',
                    'input': '{{sample.output_text}}', 'operation': 'eq', 'reference': '42'}])
            eid = definition.id
            source = {'type': 'file_content', 'content': [
                {'item': {'prompt': 'Compute 17 plus 25. Respond only with the number.'}}
                for _ in range(12)]}
            job = owner.evals.runs.create(eid, data_source={
                'type': 'responses', 'model': plan['model'], 'source': source,
                'input_messages': {'type': 'template', 'template': [
                    {'role': 'user', 'content': '{{item.prompt}}'}]}})
            rid = job.id
            expect(404, peer.evals.runs.cancel, rid, eval_id=eid)
            expect(400, owner.evals.runs.cancel, rid, eval_id=eid, extra_body={'status': 'canceled'})
            initial = owner.evals.runs.retrieve(rid, eval_id=eid).status
            if initial not in ('queued', 'in_progress'):
                raise AssertionError(f'Expected a live run, got {initial}')
        else:
            eid, rid = plan['eval_id'], plan['run_id']
            initial = owner.evals.runs.retrieve(rid, eval_id=eid).status
            assert initial == 'canceled'
        raw = owner.evals.runs.with_raw_response.cancel(rid, eval_id=eid)
        path = raw.http_response.request.url.path
        expected = f'/v1/evals/{eid}/runs/{rid}' + ('' if legacy else '/cancel')
        assert raw.status_code == 200 and path == expected
        if 'wire_output' in plan:
            # Record only the public response body, never the authenticated request headers.
            with Path(plan['wire_output']).open('xb') as capture:
                capture.write(raw.http_response.content)
        canceled = raw.parse()
        assert canceled.id == rid and canceled.status == 'canceled'
        assert owner.evals.runs.retrieve(rid, eval_id=eid).status == 'canceled'
        expect(404, peer.evals.runs.retrieve, rid, eval_id=eid)
        result = dict(sdk=openai.__version__, eval_id=eid, run_id=rid, path=path,
                      before=initial, status=canceled.status, http=raw.status_code, peer=404,
                      resource_sha256=resource_digest(raw, eid, rid))
        if legacy:
            result.update(malformed=400, foreign_cancel=404)
        else:
            if plan.get('verify_chat', True):
                reply = peer.chat.completions.create(model=plan['model'], temperature=0, max_tokens=32,
                    messages=[{'role': 'user', 'content': 'Compute 17 plus 25. Reply only with the number.'}],
                    extra_body={'enable_thinking': False})
                assert reply.choices[0].message.content.strip() == '42'
                result['fresh_chat'] = '42'
            if plan.get('delete_after', True):
                owner.evals.delete(eid)
                expect(404, owner.evals.runs.retrieve, rid, eval_id=eid)
                result['deleted'] = 404
        print(json.dumps(result))


if __name__ == '__main__':
    main()

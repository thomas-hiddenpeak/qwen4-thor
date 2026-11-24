"""Replay the four native Evals HTTP envelopes through an installed Python SDK."""
import argparse
import hashlib
import json
from pathlib import Path

import openai

try:
    import httpx2 as httpx
except ImportError:
    import httpx


def parse_response(client, route, resource):
    if route == '/evals':
        return client.evals.create(data_source_config={'type': 'logs'},
                                   testing_criteria=[])
    if route == '/evals/{eval_id}/runs':
        return client.evals.runs.create(resource['eval_id'],
                                        data_source=resource['data_source'])
    if route == '/evals/{eval_id}/runs/{run_id}':
        return client.evals.runs.retrieve(resource['id'], eval_id=resource['eval_id'])
    if route != '/evals/{eval_id}/runs/{run_id}/output_items':
        raise ValueError(f'Unexpected captured route {route}')
    rows = resource['data']
    page = client.evals.runs.output_items.list(rows[0]['run_id'], eval_id=rows[0]['eval_id'])
    for modeled, raw in zip(page.data, rows, strict=True):
        assert modeled.id == raw['id'] and modeled.status == raw['status']
        assert [v.score for v in modeled.results] == [v['score'] for v in raw['results']]
    return page


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('capture', type=Path)
    parser.add_argument('--strict', action='store_true',
                        help='Enable the private SDK response-validation option.')
    args = parser.parse_args()
    raw = args.capture.read_bytes()
    observed = json.loads(raw)['wire']
    assert len(observed) == 4
    results = []
    for envelope in observed:
        resource = envelope['value']

        def reply(request):
            assert request.method == envelope['method'].upper()
            return httpx.Response(envelope['status'], json=resource)

        with httpx.Client(transport=httpx.MockTransport(reply), trust_env=False) as transport:
            options = {'_strict_response_validation': True} if args.strict else {}
            client = openai.OpenAI(api_key='offline', base_url='http://offline.invalid/v1',
                                   max_retries=0, http_client=transport, **options)
            row = {'path': envelope['path'], 'status': envelope['status']}
            try:
                parsed = parse_response(client, envelope['path'], resource)
                if 'id' in resource:
                    assert parsed.id == resource['id']
                row['parsed'] = type(parsed).__name__
            except openai.APIResponseValidationError as exc:
                row['issues'] = [{'field': issue['loc'], 'type': issue['type']}
                                 for issue in exc.__cause__.errors()]
            results.append(row)
    print(json.dumps(dict(version=openai.__version__, strict=args.strict, cases=results,
                          capture_sha256=hashlib.sha256(raw).hexdigest())))
    return int(any('issues' in row for row in results))


if __name__ == '__main__':
    raise SystemExit(main())

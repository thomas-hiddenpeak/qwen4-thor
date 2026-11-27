"""Replay a captured ingress reply through each installed Python SDK method."""
import argparse
import hashlib
import json
import re
import warnings
from pathlib import Path

import openai

from . import sdk_route_probe as route


def error_row(exc, packet):
    challenge = exc.response.headers.get('www-authenticate')
    trace = exc.response.headers.get('x-request-id')
    actual = [exc.status_code, type(exc).__name__, exc.type, exc.code, exc.param,
              exc.request_id, challenge]
    body = packet['body']['error']
    wanted = [401, 'AuthenticationError', body['type'], body['code'],
              body['param'], packet['headers']['x-request-id'],
              packet['headers']['www-authenticate']]
    if not trace or actual != wanted:
        raise ValueError(f'Python SDK changed the received HTTP error: {actual!r}')


def target(path):
    return re.sub(r'\{[^}]+\}', route.ID, path)


def probe(packets):
    catalog = json.loads(route.CATALOG.read_bytes())
    seen = []

    def intercept(request):
        if request.url.host != 'sdk.invalid':
            raise ValueError('offline SDK probe used an unexpected origin')
        path = request.url.path
        selectors = request.url.params.get_list('beta')
        if selectors:
            path += '?' + '&'.join('beta=' + value for value in selectors)
        key = request.method + ' ' + path
        packet = packets[key]
        seen.append(key)
        return route.httpx.Response(packet['status'], headers=packet['headers'],
                                    json=packet['body'], request=request)

    rows = []
    transport = openai.DefaultHttpxClient(transport=route.httpx.MockTransport(intercept),
                                          trust_env=False)
    with openai.OpenAI(api_key='fixture', base_url='https://sdk.invalid/v1',
                       max_retries=0, http_client=transport) as client:
        for verb, path, accessor, _, _ in catalog['rows']:
            seen.clear()
            method = client
            for name in accessor.split('.'):
                method = getattr(method, name, None)
                if method is None:
                    break
            canonical = verb + ' /v1' + target(path)
            invocation = 'resource'
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', DeprecationWarning)
                try:
                    if method is None:
                        invocation = 'generic'
                        getattr(client, verb.lower())(target(path), cast_to=dict)
                    else:
                        args, kwargs = route.arguments(method, verb, path)
                        method(*args, **kwargs)
                except openai.APIStatusError as exc:
                    if len(seen) != 1:
                        raise ValueError(f'{accessor} made {len(seen)} requests') from exc
                    error_row(exc, packets[seen[0]])
                else:
                    raise ValueError(f'{accessor} did not surface the captured error')
            if seen[0] != canonical:
                legacy = verb + ' /v1' + target(catalog['legacy_cancel_path'])
                if (invocation != 'resource' or accessor != 'evals.runs.cancel'
                        or seen[0] != legacy):
                    raise ValueError(f'{accessor}: unexpected target {seen[0]}')
                invocation = 'legacy_cancel'
            rows.append([verb, path, invocation])
    return dict(language='python', version=openai.__version__, rows=rows)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('capture', type=Path)
    args = parser.parse_args()
    raw = args.capture.read_bytes()
    result = probe(json.loads(raw)['packets'])
    result['capture_sha256'] = hashlib.sha256(raw).hexdigest()
    print(json.dumps(result))

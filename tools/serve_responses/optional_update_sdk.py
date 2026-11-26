"""Exercise permitted empty resource updates over the native HTTPS listener."""

import hashlib
import http.client
import json
import ssl
import sys
import time
from urllib.parse import urlsplit

from openai import OpenAI


def main():
    plan = json.load(sys.stdin)
    base = urlsplit(plan['base'])
    assert base.scheme == 'https' and base.hostname == '127.0.0.1'
    context = ssl.create_default_context(cafile=plan['certificate'])
    model = 'qwen3.8-flash-next'

    def request(method, path, *, body=None, peer=False, media_type=None):
        # Keep this contract check within the fixture's 20 requests/s limit.
        time.sleep(0.065)
        headers = {'Authorization': 'Bearer ' + plan['other' if peer else 'key']}
        if path.startswith('/agents'):
            headers['OpenAI-Beta'] = 'agents=v1'
        if media_type:
            headers['Content-Type'] = media_type
        connection = http.client.HTTPSConnection(base.hostname, base.port,
                                                context=context, timeout=30)
        try:
            connection.request(method, base.path + path, body=body, headers=headers)
            reply = connection.getresponse()
            return reply.status, json.loads(reply.read())
        finally:
            connection.close()

    def create(path, fields):
        status, data = request('POST', path, body=json.dumps(fields).encode(),
                               media_type='application/json')
        assert status in (200, 201), (path, status, data.get('error'))
        return path + '/' + data['id']

    template = '/agents/environments/templates'
    agents = '/agents'
    sessions = agents + '/sessions'
    hooks = '/webhook_endpoints'
    old_session = plan['historical_session']
    assert old_session['id'].startswith('agent_session_')
    session_path = sessions + '/' + old_session['id']
    paths = [create(agents, {'model': model, 'metadata': {'stage': 'input-contract'}}),
             create(template, {'name': 'Thor release query'}),
             session_path,
             create(hooks, {'name': 'Local contract fixture', 'url': plan['subscriber'],
                            'event_types': ['response.completed']})]
    def fields(resource):
        return {key: value for key, value in resource.items() if key != "updated_at"}

    rows = []
    for path in paths:
        status, initial = request('GET', path)
        assert status == 200 and 'signing_secret' not in initial
        original = json.dumps(fields(initial), sort_keys=True, separators=(',', ':')).encode()
        if path == session_path:
            assert hashlib.sha256(original).hexdigest() == old_session['sha256']
        latest = initial
        for body, media_type in ((None, None), (b'', 'application/json'), (b'{}', 'application/json')):
            status, result = request('POST', path, body=body, media_type=media_type)
            assert status == 200 and fields(result) == fields(initial), (path, status, result.get("error"))
            if "updated_at" in latest:
                assert result["updated_at"] >= latest["updated_at"]
            latest = result
        foreign, _ = request('POST', path, peer=True)
        null, _ = request('POST', path, body=b'null', media_type='application/json')
        malformed, _ = request('POST', path, body=b'{', media_type='application/json')
        bad_media, _ = request('POST', path, body=b'{}', media_type='text/plain')
        assert (foreign, null, malformed, bad_media) == (404, 400, 400, 415), (path, foreign, null, malformed, bad_media)
        assert request('GET', path) == (200, latest)
        rows.append(dict(path=path, stable_fields_sha256=hashlib.sha256(original).hexdigest(),
                         post=[200, 200, 200], foreign=foreign, null=null,
                         malformed=malformed, invalid_media_type=bad_media))
    assert request('DELETE', paths[-1])[0] == 200
    controls = {}
    for path in (agents, sessions, hooks, paths[2] + '/events'):
        status, _ = request('POST', path, body=b'', media_type='application/json')
        assert status == 400, (path, status)
        controls[path] = status

    # Use the SDK's configured CA and type checking for a normal inference.
    try:
        import httpx2 as httpx
    except ImportError:
        import httpx
    with OpenAI(base_url=plan['base'], api_key=plan['key'], max_retries=0,
                _strict_response_validation=True, http_client=httpx.Client(verify=context)) as client:
        saved = client.beta.agents.sessions.retrieve(paths[2].rsplit('/', 1)[1])
        assert saved.status == 'idle'
        failed = client.beta.agents.sessions.turns.retrieve(old_session['failed_turn'], session_id=saved.id)
        assert failed.status == 'failed' and failed.error.code == 'rate_limit_exceeded'
        result = client.chat.completions.create(model=model, temperature=0, max_tokens=32,
            reasoning_effort='none',
            messages=[{'role': 'user', 'content': 'Return only the integer product of 6 and 7.'}])
        answer = result.choices[0].message.content.strip()
        assert answer == '42', answer
    print(json.dumps(dict(resources=rows, required_bodies=controls, historical_failed_turn=failed.id, independent_chat=answer)))


if __name__ == '__main__':
    main()

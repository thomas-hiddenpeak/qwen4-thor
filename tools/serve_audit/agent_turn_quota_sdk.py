"""Replay a saved Agent quota failure through the installed Python SDK."""

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import openai

try:
    import httpx2 as httpx
except ImportError:
    import httpx


ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / 'docs/evidence/serve-responses-errors-20261126'
FIXTURE = EVIDENCE / 'agent-quota-replay.json'


def digest(content):
    return hashlib.sha256(content).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--strict', action='store_true')
    args = parser.parse_args()
    fixture = json.loads(FIXTURE.read_bytes())
    live = (EVIDENCE / 'agent-turn-http.json').read_bytes()
    assert digest(live) == fixture['original_tls_receipt_sha256']
    node = json.loads(live)['events']
    replies = fixture['http']
    for reply in replies.values():
        assert digest(reply['body'].encode()) == reply['body_sha256']
    saved = json.loads(replies['turn']['body'])
    session_id, turn_id = saved['session_id'], saved['id']
    resource = '/agents/sessions/' + session_id
    events = fixture['quota_projection']
    expected_ids = [(e['type'], e['id']) for e in node['events']]
    assert [(e['type'], e['event_id']) for e in events] == expected_ids
    assert saved['id'] == node['failed_turn']
    assert saved['error'] == node['read_error'] == node['event_error']
    frames = ''.join(f"id: {item['event_id']}\r\nevent: {item['type']}\r\n"
                     f"data: {json.dumps(item, ensure_ascii=False)}\r\n\r\n" for item in events)
    calls = []

    def serve(request):
        assert request.method == 'GET' and request.url.host == 'replay.invalid'
        assert request.headers['OpenAI-Beta'] == 'agents=v1'
        path = request.url.path.removeprefix('/v1')
        calls.append(path)
        if path == resource + '/events':
            assert request.headers['Accept'] == 'text/event-stream'
            return httpx.Response(200, headers={'Content-Type': 'text/event-stream'},
                                  content=frames.encode())
        if path == resource + '/turns':
            assert dict(request.url.params) == {'order': 'asc', 'limit': '100'}
        foreign = request.headers['Authorization'] == 'Bearer replay-other'
        label = {resource: 'session', resource + '/turns': 'turns',
                 resource + '/turns/' + turn_id: 'foreign' if foreign else 'turn'}[path]
        reply = replies[label]
        return httpx.Response(reply['status'], headers={'Content-Type': 'application/json'},
                              content=reply['body'].encode())

    def client(key):
        return openai.OpenAI(api_key=key, base_url='https://replay.invalid/v1', max_retries=0,
                             _strict_response_validation=args.strict,
                             http_client=httpx.Client(transport=httpx.MockTransport(serve)))

    options = {'headers': {'OpenAI-Beta': 'agents=v1'}}
    with client('replay-owner') as api:
        typed = hasattr(api.beta, 'agents')
        if typed:
            sessions = api.beta.agents.sessions
            session = sessions.retrieve(session_id).to_dict()
            turn = sessions.turns.retrieve(turn_id, session_id=session_id).to_dict()
            page = sessions.turns.list(session_id, order='asc', limit=100)
            assert not page.has_next_page()
            turns = [row.to_dict() for row in page.data]
            with sessions.events.stream(session_id) as stream:
                decoded = [event.to_dict() for event in stream]
        else:
            session = api.get(resource, cast_to=dict[str, Any], options=options)
            turn = api.get(resource + '/turns/' + turn_id, cast_to=dict[str, Any], options=options)
            page = api.get(resource + '/turns', cast_to=dict[str, Any],
                           options={**options, 'params': {'order': 'asc', 'limit': 100}})
            assert page['has_more'] is False
            turns = page['data']
            stream_options = {'headers': {**options['headers'], 'Accept': 'text/event-stream'}}
            with api.get(resource + '/events', cast_to=dict[str, Any], options=stream_options,
                         stream=True, stream_cls=openai.Stream[dict[str, Any]]) as stream:
                decoded = list(stream)
    assert session == json.loads(replies['session']['body'])
    assert turns == json.loads(replies['turns']['body'])['data']
    assert turn == saved and turns[1] == turn
    assert [item['status'] for item in turns] == ['completed', 'failed', 'completed']
    assert decoded == events
    assert decoded[-2]['turn']['error'] == turn['error']
    assert turn['error']['code'] == 'rate_limit_exceeded'
    assert decoded[-1]['session']['status'] == session['status'] == 'idle'
    with client('replay-other') as peer:
        try:
            if typed:
                peer.beta.agents.sessions.turns.retrieve(turn_id, session_id=session_id)
            else:
                peer.get(resource + '/turns/' + turn_id, cast_to=dict[str, Any], options=options)
        except openai.NotFoundError as error:
            assert error.status_code == 404 and error.response.json() == json.loads(replies['foreign']['body'])
        else:
            raise AssertionError('another client read the turn')
    assert len(calls) == 5
    print(json.dumps(dict(sdk=openai.__version__, strict=args.strict,
                          interface='beta.agents.sessions' if typed else 'generic GET and Stream[dict[str, Any]]',
                          requests=len(calls), events=len(decoded), error=turn['error']['code'],
                          terminal=session['status'], peer_status=404, input_sha256=digest(FIXTURE.read_bytes()))))


if __name__ == '__main__':
    main()

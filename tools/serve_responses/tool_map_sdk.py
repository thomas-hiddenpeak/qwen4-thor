"""Read two real release records through a model-selected dictionary argument."""
import hashlib
import http.client
import importlib.metadata
import json
import ssl
import sys
from pathlib import Path
from urllib.parse import urlsplit

from openai import OpenAI


def read_scoped(cfg, method, path, *, peer=False):
    origin = urlsplit(cfg['base'])
    headers = {'Authorization': 'Bearer ' + cfg['other' if peer else 'key']}
    if path.startswith('/agents/'):
        headers['OpenAI-Beta'] = 'agents=v1'
    if origin.scheme == 'https':
        context = ssl.create_default_context(cafile=cfg['certificate'])
        conn = http.client.HTTPSConnection(origin.hostname, origin.port, context=context, timeout=30)
    else:
        assert origin.scheme == 'http' and origin.hostname == '127.0.0.1' and not cfg['certificate']
        conn = http.client.HTTPConnection(origin.hostname, origin.port, timeout=30)
    try:
        conn.request(method, origin.path + path, headers=headers)
        reply = conn.getresponse()
        return reply.status, reply.read()
    finally:
        conn.close()


def stored_resources(cfg, agent_id, chat_id, call_id, args):
    agent_path = '/agents/' + agent_id
    chat_path = '/chat/completions/' + chat_id
    status, raw = read_scoped(cfg, 'GET', chat_path)
    assert status == 200
    chat = json.loads(raw)
    call = chat['choices'][0]['message']['tool_calls'][0]
    assert chat['id'] == chat_id and call['id'] == call_id
    assert call['function']['name'] == 'lookup_release' and json.loads(call['function']['arguments']) == args
    status, before = read_scoped(cfg, 'GET', agent_path)
    assert status == 200
    status, after = read_scoped(cfg, 'POST', agent_path)
    assert status == 200
    original, updated = json.loads(before), json.loads(after)
    assert original['id'] == updated['id'] == agent_id and updated['metadata']['lookup'] == call_id
    assert updated['updated_at'] >= original['updated_at']
    stable = {k: v for k, v in original.items() if k != 'updated_at'}
    assert stable == {k: v for k, v in updated.items() if k != 'updated_at'}
    assert read_scoped(cfg, 'POST', agent_path, peer=True)[0] == 404
    assert read_scoped(cfg, 'GET', chat_path, peer=True)[0] == 404
    return dict(agent_id=agent_id, chat_id=chat_id, call_id=call_id,
                chat_sha256=hashlib.sha256(raw).hexdigest(), owner_patch=200, foreign=404,
                agent_fields_sha256=hashlib.sha256(json.dumps(stable,sort_keys=True).encode()).hexdigest())


def main():
    cfg = json.load(sys.stdin)
    capture = Path(cfg['capture'])
    capture.mkdir(mode=0o700)
    origin = urlsplit(cfg['base'])
    assert origin.hostname == '127.0.0.1'
    handoff = bool(cfg.get('handoff'))
    assert origin.scheme == 'https' or (handoff and origin.scheme == 'http' and not cfg['certificate'])
    records = {}
    for name in ('primary', 'previous'):
        raw = Path(cfg['release_records'][name]).read_bytes()
        publication = json.loads(raw)['publication']
        assert publication['status'] == 'PASS' and publication['desired'] == 'STOPPED'
        records[name] = dict(revision=publication['publication_revision'],
                             protected=publication['protected_files'],
                             source_sha256=hashlib.sha256(raw).hexdigest())
    versions = {name: row['revision'] for name, row in records.items()}
    assert versions['primary'] > versions['previous']
    tool = dict(type='function', function=dict(name='lookup_release', strict=False,
        description='Look up the indicated published revisions in the project release records.',
        parameters={'type': 'object', '$defs': {'revision': {'type': 'integer', 'minimum': 1}},
                    'properties': {'versions': {'type': 'object', 'additionalProperties': {
                        '$ref': '#/$defs/revision'}}},
                    'required': ['versions'], 'additionalProperties': False}))
    task = (f"Call lookup_release with versions containing primary={versions['primary']} and "
            f"previous={versions['previous']}. After receiving the records, tell me only the "
            "integer count of protected files from the primary release.")
    initial = [{'role': 'user', 'content': task}]
    try:
        import httpx2 as httpx
    except ImportError:
        import httpx
    context = ssl.create_default_context(cafile=cfg['certificate'])
    saved, restored = None, None
    with OpenAI(base_url=cfg['base'], api_key=cfg['key'], max_retries=0,
                _strict_response_validation=True, http_client=httpx.Client(verify=context)) as api:
        common = dict(model='qwen3.8-flash-next', temperature=0, reasoning_effort='none',
                      max_tokens=160, tools=[tool])
        if handoff:
            common['store'] = True
            if previous := cfg.get('previous'):
                restored = stored_resources(cfg, previous['agent_id'], previous['chat_id'],
                                            previous['call_id'], {'versions':versions})
                assert restored == previous
        response = api.chat.completions.create(**common, messages=initial,
            tool_choice={'type':'function','function':{'name':'lookup_release'}})
        (capture/'lookup.json').write_text(response.model_dump_json(exclude_unset=True))
        choice = response.choices[0]
        assert choice.finish_reason == 'tool_calls' and len(choice.message.tool_calls) == 1
        call = choice.message.tool_calls[0]
        assert call.id and call.function.name == 'lookup_release'
        def distinct(pairs):
            values = dict(pairs)
            assert len(values) == len(pairs), 'duplicate tool argument'
            return values
        args = json.loads(call.function.arguments, object_pairs_hook=distinct)
        assert args == {'versions': versions} and all(type(v) is int for v in args['versions'].values()), args
        results = {}
        for name, revision in args['versions'].items():
            raw = Path(cfg['release_records'][name]).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == records[name]['source_sha256']
            row = json.loads(raw)['publication']
            assert row['publication_revision'] == revision
            results[name] = dict(revision=revision, protected_files=row['protected_files'])
        history = initial + [
            dict(role='assistant', content=choice.message.content, tool_calls=[dict(
                id=call.id, type='function', function=dict(name=call.function.name,
                                                          arguments=call.function.arguments))]),
            dict(role='tool', tool_call_id=call.id, content=json.dumps(results))]
        (capture/'tool-output.json').write_text(json.dumps(results))
        chunks, endings = [], []
        with api.chat.completions.create(**common, messages=history, tool_choice='none',
                                         stream=True) as stream, (capture/'followup.jsonl').open('x') as events:
            for event in stream:
                events.write(event.model_dump_json(exclude_unset=True) + '\n')
                for item in event.choices:
                    if item.delta.content: chunks.append(item.delta.content)
                    if item.finish_reason: endings.append(item.finish_reason)
        answer = ''.join(chunks).strip()
        assert endings == ['stop'] and answer == str(records['primary']['protected']), (endings, answer)
        if handoff:
            agent = api.beta.agents.create(model=common['model'], metadata={'lookup':call.id})
            saved = stored_resources(cfg, agent.id, response.id, call.id, args)
    print(json.dumps(dict(saved=saved, restored=restored, sdk=importlib.metadata.version('openai'), call_id=call.id,
                          arguments=args, records=records, reply=answer,
                          finish_reasons=[choice.finish_reason, *endings], text_deltas=len(chunks))))


if __name__ == '__main__':
    main()

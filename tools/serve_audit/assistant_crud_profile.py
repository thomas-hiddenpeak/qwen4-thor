"""Real project-release readback through scoped Assistants, Threads and Messages."""
import hashlib
import json
import re
from urllib.parse import parse_qsl


def make_profile(source):
    digest = hashlib.sha256(source).hexdigest()
    info = json.loads(source)['publication']
    metadata = {'source_sha256':digest, 'revision':str(info['publication_revision'])}
    updated = {**metadata,'desired':info['desired']}
    excerpt = json.dumps({k:info[k] for k in ('publication_revision','desired','engine')},
                         ensure_ascii=False, sort_keys=True)
    steps = []

    def step(name, call, args=(), fields=None, *, save=None, other=False, miss=False, parent=False):
        steps.append(dict(name=name, call=call, args=list(args), fields=fields or {}, save=save,
                          scope='other' if other else 'owner', status=404 if miss else 200, parent=parent))

    step('assistant-create', 'assistants.create', fields=dict(model='qwen3.8-flash-next',
         name='q4t release records', metadata=metadata), save='assistant')
    step('assistant-update', 'assistants.update', ['assistant'], dict(metadata=updated))
    step('assistant-read', 'assistants.retrieve', ['assistant'])
    step('assistant-list', 'assistants.list', fields=dict(limit=20,order='desc'))
    step('assistant-foreign', 'assistants.retrieve', ['assistant'], other=True, miss=True)
    step('thread-create', 'threads.create', fields=dict(metadata=metadata), save='thread')
    step('thread-update', 'threads.update', ['thread'], dict(metadata=updated))
    step('thread-read', 'threads.retrieve', ['thread'])
    step('thread-foreign', 'threads.retrieve', ['thread'], other=True, miss=True)
    step('message-create', 'messages.create', ['thread'], dict(role='user',content=excerpt,
         metadata=metadata), save='message')
    step('message-update', 'messages.update', ['message'], dict(metadata=updated), parent=True)
    step('message-read', 'messages.retrieve', ['message'], parent=True)
    step('message-list', 'messages.list', ['thread'], dict(limit=20,order='desc'))
    step('message-foreign', 'messages.retrieve', ['message'], parent=True, other=True, miss=True)
    step('message-delete', 'messages.delete', ['message'], parent=True)
    step('message-deleted', 'messages.retrieve', ['message'], parent=True, miss=True)
    step('thread-delete', 'threads.delete', ['thread'])
    step('thread-deleted', 'threads.retrieve', ['thread'], miss=True)
    step('assistant-delete', 'assistants.delete', ['assistant'])
    step('assistant-deleted', 'assistants.retrieve', ['assistant'], miss=True)
    return steps


def verify(actor, label, source):
    if not __debug__:
        raise RuntimeError('Verification requires assertions enabled')
    steps = make_profile(source)
    fields = {step['name']: step['fields'] for step in steps}
    ids = actor['ids']
    assert actor['sdk'] == label and len(actor['requests']) == len(steps)
    assert set(ids) == {'assistant','thread','message'}
    assert all(isinstance(ids[k],str) and ids[k].startswith(p) for k,p in
               [('assistant','asst_'),('thread','thread_'),('message','msg_')])
    paths = {'assistant':'/v1/assistants','thread':'/v1/threads',
             'message':'/v1/threads/' + ids['thread'] + '/messages'}
    types = {'assistant':'assistant','thread':'thread','message':'thread.message'}
    traces = set()
    for step, row in zip(steps,actor['requests']):
        entity, action = step['name'].split('-')
        method = {'create':'POST','update':'POST','delete':'DELETE'}.get(action,'GET')
        path = paths[entity] + ('' if action in {'list','create'} else '/'+ids[entity])
        assert (row['method'],row['path'],row['scope'],row['status'],row['step']) == (
            method,path,step['scope'],step['status'],step['name']), step['name']
        assert row['beta'] == 'assistants=v2', step['name']
        trace = row['trace']
        assert isinstance(trace,str) and re.fullmatch(r'req-[a-zA-Z0-9-]+', trace) and trace not in traces
        traces.add(trace)
        query = sorted(parse_qsl(row['query'], strict_parsing=True, keep_blank_values=True))
        assert query == ([('limit','20'),('order','desc')] if action == 'list' else []), step['name']
        if method == 'POST': assert row['body'] == step['fields'], step['name']
        else: assert row['body'] is None
        wire, typed = row['response'], row['sdk_value']
        if step['status'] == 404:
            code = 'message_not_found' if step['name'] == 'message-deleted' else 'object_not_found'
            assert typed == {'status':404} and wire['error']['code'] == code, step['name']
        elif action == 'list':
            values = [v['id'] for v in wire['data']]
            assert wire['object'] == 'list' and typed == {'object':'list','ids':values}
            found = [v for v in wire['data'] if v['id'] == ids[entity]]
            assert len(found) == 1 and found[0]['metadata'] == fields['assistant-update']['metadata']
        else:
            for result in (typed, wire):
                assert result['id'] == ids[entity]
                if action == 'delete':
                    assert result['deleted'] is True and result['object'] == types[entity]+'.deleted'
                    continue
                assert result['object'] == types[entity]
                assert result['metadata'] == fields[entity + ('-create' if action == 'create' else '-update')]['metadata']
                if entity == 'message':
                    assert result['role'] == 'user' and result['thread_id'] == ids['thread']
                    assert result['content'][0]['text']['value'] == fields['message-create']['content']
                if entity == 'assistant':
                    assert result['model'] == fields['assistant-create']['model']
    return traces

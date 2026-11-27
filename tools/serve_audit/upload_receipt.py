"""Recheck a persisted multipart handoff against its original document and HTTP trace."""
from collections import Counter
import hashlib
import json


def require(condition, message):
    if not condition:
        raise ValueError(message)


def verify(source, rows, takeover, runtime, handoff):
    release = json.loads(source)
    require(release['status'] == 'PASS' and release['desired'] == 'STOPPED', 'release not stable')
    require(runtime['status'] == 'PASS' and runtime['model_starts'] == 2 and
            runtime['unchanged_default'] and runtime['ports_free'] and not runtime['remaining'] and
            runtime['supervisor_exit'] == runtime['stop_returncode'] == 0, 'managed cleanup')
    require(handoff['guardian_same'] and (handoff['old_generation'], handoff['new_generation']) == (1, 2) and
            handoff['primary'] == release['primary'] == runtime['receipt_sha256'] and
            handoff['fallback'] == release['fallback'] != release['primary'] and
            takeover['payload_manifest_sha256'] == release['fallback'] and takeover['selected'] == 'fallback' and
            takeover['probe_count'] == 1 and takeover['fallback_consumed'] and takeover['last_fault']['signal'] == 6, 'registered package handoff')
    require(len(rows) == handoff['sdk_requests'] == 25 and handoff['independent_chats'] == 1 and
            [r['step'] for r in rows] == list(range(1, 26)), 'request accounting')
    wire = [row['wire'] for row in rows]
    traces = {r['trace'] for r in wire}
    require(len(traces) == len(wire) and all(isinstance(t, str) and t for t in traces), 'request identities')
    require({row['sdk'] for row in rows} == {f'python-{v}' for v in ('2.8.1','2.33.0','3.42.1','3.55.1')} |
            {'node-6.34.0','node-7.47.0'}, 'SDK coverage')
    data = [row['data'] for row in rows]
    first, second, aborted = [data[i]['id'] for i in (0, 4, 6)]
    file, resumed, direct = data[3]['file']['id'], data[13]['file']['id'], data[7]['id']
    require(len({first, second, aborted, file, resumed, direct}) == 6 and
            (handoff['completed'], handoff['resumed']) == (file, resumed), 'resource identities')
    resources = (f'/uploads/{first}', f'/uploads/{second}', f'/uploads/{aborted}')
    a,b,c = resources
    f,g,h = ('/files/'+identity for identity in (file, resumed, direct))
    expected = [
        ('POST','/uploads'), ('POST',a+'/parts'), ('POST',a+'/parts'), ('POST',a+'/complete'),
        ('POST','/uploads'), ('POST',b+'/parts'), ('POST','/uploads'), ('POST','/files'),
        ('POST',a+'/complete'), ('GET',f), ('GET',f+'/content'), ('POST',b+'/parts'),
        ('POST',b+'/complete'), ('POST',b+'/complete'), ('GET',g+'/content'), ('GET',g),
        ('POST',c+'/cancel'), ('POST',c+'/parts'), ('GET','/files'), ('POST','/responses'),
        ('DELETE',f), ('DELETE',g), ('DELETE',h), ('GET',g), ('GET','/files')]
    for step, (r, (method,path)) in enumerate(zip(rows,expected), 1):
        code = {13:400,16:404,18:409,24:404}.get(step, 200)
        require((r['wire']['method'], r['wire']['path'], r['wire']['status'], r['owner']) ==
                (method, '/v1'+path, code, 'beta' if step == 16 else 'alpha'), f'wire step {step}')
    require(data[3] == data[8] and data[3]['file'] == data[9] and data[3]['id'] == first and
            data[13]['id'] == second and data[3]['status'] == data[13]['status'] == 'completed',
            'replayed completion or resumed file')
    digest = hashlib.sha256(source).hexdigest()
    require(data[10] == data[14] == dict(bytes=len(source), sha256=digest) and
            handoff['content_sha256'] == digest, 'downloaded source')
    require(data[16]['id'] == aborted and data[16]['status'] == 'cancelled' and
            data[17]['error'] == 'upload_not_pending' and data[12]['error'] == 'invalid_request', 'upload lifecycle')
    require(len(data[18]['ids']) == 3 and set(data[18]['ids']) == {file,resumed,direct} and
            not data[18]['has_more'] and data[24] == dict(ids=[],has_more=False), 'file uniqueness or cleanup')
    answer = str(release['version_revision'])
    require(data[19]['status'] == 'completed' and data[19]['answer'].strip() == answer == handoff['answer'],
            'project record answer')
    return dict(sdk_requests=len(rows), response_answer=answer, download_bytes=len(source),
                download_sha256=digest, codes=dict(Counter(r['status'] for r in wire)))

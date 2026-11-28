"""Qualify native vector indexing and the diagnostics for every persisted chunk."""
import hashlib
import http.client
import json
from pathlib import Path
import re
import ssl
import time

from tools.serve_recovery import profile_live_reuse as history, profile_record
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_service import package

ROOT = lifecycle.ROOT
KEY = 'vector_ingestion_outcomes'
RECEIPT = 'vector-ingestion-outcomes.json'
DIRECTORY = ROOT/'docs/evidence/serve-vector-ingestion-diagnostics-20261128'
DOCUMENT = DIRECTORY/'cpu.json'
PROOF = DIRECTORY/'device.json'
POLL_LIMIT = 24
# Four setup/read calls, an ownership check and four cleanup/retrieval checks.
MAX_CALLS = 9 + POLL_LIMIT


def prepare(out, build, deployment):
    proof = package.read(PROOF)
    package.require(proof['status'] == 'PASS' and proof['actual_chunks'] > 1
                    and proof['jobs'] == 2 and proof['nvml_samples'] > 0
                    and proof['candidate'] == package.sha(build/'q4t')
                    and proof['previous_engine'] == package.sha(deployment/'build/q4t')
                    and proof['admission_sha256'] == package.sha(DIRECTORY/'admission.json'),
                    'Vector diagnosis device or admission proof differs')
    source = (deployment/'build/service').resolve(strict=True)
    prior = package.read(source/'manifest.json')
    package.require(package.sha(build/'q4t-ingress') == package.sha(source/'bin/q4t-ingress')
                    and all(prior['recovery_profiles']['qualifications'][role]['evidence_sha256']
                            == history.STORE_VIEWS_REVIEWED[role] for role in ('primary','light')),
                    'Registered ingress or capacity source differs')
    lifecycle.prepare_from_manifest(out, build, deployment, package.sha(source/'manifest.json'),
                                    history.STORE_VIEWS_REVIEWED, KEY, (Path(__file__), DOCUMENT, PROOF))
    for name in ('primary', 'light'):
        path = out/name/'plan.json'
        plan = package.read(path)
        plan['limits'][KEY] = 1
        package.save(path,plan)


def snapshot_evidence(objects, store, file_id, public):
    matches = []
    for path in objects.glob('*.vector_store_file.*.json'):
        data = package.read(path)['record']['data']
        file = data['file']
        if file['id'] == file_id and file['vector_store_id'] == store:
            matches.append(data)
    package.require(len(matches) == 1, 'Expected one stored file in this vector store')
    data = matches[0]
    job, file, identity = data['job'], data['file'], data['index_identity']
    ref, = data['input_snapshots']
    snapshot, = objects.glob('*.' + ref['snapshot']['id'] + '.json')
    record = package.read(snapshot)['record']['data']
    _, text, vectors = record['segments']
    package.require(public['status'] == file['status'] == 'completed'
                    and public['id'] == identity['file_id'] == file_id
                    and public['vector_store_id'] == store and public['last_error'] is None
                    and file['usage_bytes'] == public['usage_bytes'] == text['bytes'] + vectors['bytes']
                    and identity['source_sha256'] == package.sha(DOCUMENT)
                    and identity['text_sha256'] == text['sha256']
                    and record['format'] == 'q4t.input_snapshot.v1'
                    and record['binding_sha256'] == ref['binding_sha256']
                    and job['state'] == 'terminal' and job['attempts'] == 1
                    and re.fullmatch(r'tool_run_[0-9a-f]{32}', job['token'])
                    and type(identity['dimensions']) is int and identity['dimensions'] > 0,
                    'Public job, stored document, index binding or data charge differs')
    chunk_count, rest = divmod(vectors['bytes'], 4 * identity['dimensions'])
    package.require(rest == 0 and chunk_count > 1, 'Incomplete or single-chunk test index')
    return dict(job=job['token'], store=store, file=file_id, source_sha256=identity['source_sha256'],
                dimensions=identity['dimensions'], vectors_bytes=vectors['bytes'], chunks=chunk_count,
                binding=ref['binding_sha256'], usage_bytes=file['usage_bytes'])


def terminal_rows(lines):
    rows = []
    for line in lines:
        if '[q4t][request]' not in line or ' event=end ' not in line:
            continue
        fields = dict(word.split('=',1) for word in line.split() if '=' in word)
        if fields['trace_id'].startswith('tool_run_'):
            rows.append({k:fields[k] for k in ('instance','id','trace_id','outcome','reason','released','seq')})
    return rows


def check_terminals(index, rows):
    token = index['job']
    package.require(len(rows) == index['chunks'] == len({r['instance'] for r in rows})
                    and len(rows) > 1 and index['vectors_bytes'] == 4 * index['dimensions'] * len(rows)
                    and all(r['id'] == r['trace_id'] == token
                            and r['instance'].isdecimal() and r['seq'].isdecimal()
                            and (r['outcome'],r['reason'],r['released']) == ('success','execution_complete','1')
                            for r in rows), 'Persisted chunks differ from successful released executions')


def request(stage, method, path, data, mime, client):
    ctx = ssl.create_default_context(cafile=stage.plan['tls']['certificate'])
    conn = http.client.HTTPSConnection('127.0.0.1',stage.plan['public_port'],context=ctx,timeout=20)
    try:
        headers = {'Authorization':'Bearer '+stage.keys[client]}
        if mime:
            headers['Content-Type'] = mime
        conn.request(method,path,body=data,headers=headers)
        response = conn.getresponse()
        raw = response.read(65537)
        package.require(len(raw) <= 65536, 'Capacity response exceeds its read bound')
        return response.status, response.getheader('x-request-id'), raw
    finally:
        conn.close()


def ingest(stage, objects):
    rows = []
    document = DOCUMENT.read_bytes()
    boundary = 'q4t-vector-ingestion-capacity'
    package.require(boundary.encode() not in document and len(document) <= 32768, 'Unexpected source document')
    upload = (f'--{boundary}\r\nContent-Disposition: form-data; name="purpose"\r\n\r\nassistants\r\n'
              f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="diagnostics.txt"\r\n'
              'Content-Type: text/plain\r\n\r\n').encode() + document + f'\r\n--{boundary}--\r\n'.encode()
    def call(method, path, body=None, status=200, client='alpha', binary=False):
        package.require(len(rows) < MAX_CALLS, 'Exceeded capacity HTTP budget')
        if isinstance(body, bytes):
            data, mime = body, 'multipart/form-data; boundary='+boundary
        else:
            data = None if body is None else json.dumps(body).encode()
            mime = None if body is None else 'application/json'
        code, trace, raw = request(stage, method, path, data, mime, client)
        package.require(code == status and trace, f'{method} {path} returned {code}, expected {status}')
        rows.append(dict(method=method,path=path,status=code,trace=trace,client=client))
        return raw if binary else json.loads(raw)

    store = call('POST','/v1/vector_stores',{'name':'q4t-'+stage.plan['name']})['id']
    file = call('POST','/v1/files',upload)['id']
    base = '/v1/vector_stores/'+store
    path = base+'/files/'+file
    created = call('POST',base+'/files',dict(file_id=file,chunking_strategy={
        'type':'static','static':{'max_chunk_size_tokens':256,'chunk_overlap_tokens':16}}))
    package.require(created['id'] == file and created['vector_store_id'] == store, 'Different attached file')
    for _ in range(POLL_LIMIT):
        value = call('GET',path)
        if value['status'] != 'in_progress':break
        time.sleep(.75)
    index = snapshot_evidence(objects,store,file,value)
    error = call('GET',base,status=404,client='beta')
    package.require(error['error']['code'] and 'name' not in error, 'Foreign store data disclosed')
    payload = call('GET','/v1/files/'+file+'/content',binary=True)
    package.require(payload == document, 'Retrieved a different source document')
    for target, ident in [(base,store),('/v1/files/'+file,file)]:
        deleted = call('DELETE',target)
        package.require(deleted['id'] == ident and deleted['deleted'] is True, 'Object deletion failed')
        call('GET',target,status=404)
    verify_wire(index,rows)
    return dict(index=index,requests=rows,source_sha256=hashlib.sha256(payload).hexdigest())


def verify_wire(index, rows):
    store = '/v1/vector_stores/'+index['store']
    file = '/v1/files/'+index['file']
    count = len(rows) - 9
    package.require(1 <= count <= POLL_LIMIT, 'Unexpected polling count')
    def op(method, path, status=200, client='alpha'):
        return (method,path,status,client)
    wanted = [op('POST','/v1/vector_stores'),op('POST','/v1/files'),op('POST',store+'/files')]
    wanted += [op('GET',store+'/files/'+index['file'])] * count
    wanted += [op('GET',store,404,'beta'),op('GET',file+'/content'),
               op('DELETE',store),op('GET',store,404),op('DELETE',file),op('GET',file,404)]
    package.require([(r['method'],r['path'],r['status'],r['client']) for r in rows] == wanted
                    and all(isinstance(r['trace'],str) and r['trace'] for r in rows)
                    and len({r['trace'] for r in rows}) == len(rows), 'Capacity HTTP records differ')


def business(stage):
    stage.budget(KEY)
    args = stage.plan['command']
    objects = Path(args[args.index('--object-store-dir')+1])
    package.require(objects == stage.out.parent/'state/application-objects','Wrong capacity object store')
    result = ingest(stage,objects)
    with (stage.out/'model.log').open() as log:
        result['terminals'] = terminal_rows(log)
    check_terminals(result['index'],result['terminals'])
    package.save(stage.out/RECEIPT,result)
    stage.same_model()


def verify(phase, plan, capabilities, current):
    proof = package.read(PROOF)
    change, name = plan[KEY], plan['name']
    prior = profile_record.read(phase/'previous-profile.json')
    expected = dict(prior['capabilities'], protocol_source_sha256=change['protocol'])
    result = package.read(phase/RECEIPT)
    check_terminals(result['index'], result['terminals'])
    verify_wire(result['index'],result['requests'])
    hashes = prior['business']['live']['evidence_sha256']
    package.require(package.sha(phase/'previous-profile.json') == change['previous'] == history.STORE_VIEWS_REVIEWED[name]
                    and plan['selected_sha256'] == change['release']
                    and package.same_json(capabilities, expected) and package.same_json(plan['capacity'],prior['capacity'])
                    and proof['candidate'] == plan['binary_sha256']
                    and package.read(phase/'result.json')['counts'][KEY] == plan['limits'][KEY] == 1
                    and result['source_sha256'] == result['index']['source_sha256'] == package.sha(DOCUMENT)
                    and history.STORE_VIEWS_RECEIPT in hashes
                    and all(plan['sources'][str(path.relative_to(ROOT))]==package.sha(path)
                            for path in (Path(__file__),PROOF,DOCUMENT)),
                    'Changed capacity, inputs, engine or predecessor evidence')
    return {**{k:v for k,v in hashes.items() if k not in current}, RECEIPT:package.sha(phase/RECEIPT)}

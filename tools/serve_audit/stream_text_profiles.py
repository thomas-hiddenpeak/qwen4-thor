"""Verify the public Responses stream across primary and light capacities."""
import copy
import json
import os
from pathlib import Path
import subprocess

from tools.serve_recovery import profile_live_reuse as reuse, profile_record
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_responses.web_provider_acceptance import Witness
from tools.serve_service import package

ROOT = Path(__file__).resolve().parents[2]
KEY = 'responses_stream_text_handoff'
RECEIPT = 'responses-stream-text-handoff.json'
PROOF = ROOT/'docs/evidence/serve-responses-search-stream-sdk-20261128/device.json'
QUESTION = ROOT/'docs/evidence/serve-web-provider-20261128/responses.request.json'
ACTOR = ROOT/'tools/serve_audit/response_stream_text.mjs'
INPUTS = (Path(__file__), ACTOR, PROOF, QUESTION)


def prepare(out, build, deployment, policy_file, sdk):
    proof = package.read(PROOF)
    package.require(proof['engine'] == package.sha(build/'q4t') and
                    proof['ingress'] == package.sha(build/'q4t-ingress') and
                    proof['protocol_sources'] == package.sha(build/'generated/q4t/server/chat_protocol_sources.json') and
                    proof['guard']['status'] == 'COMPLETED',
                    'candidate differs from the real TLS stream qualification')
    policy = package.read(policy_file)
    binding = package.web_policy_binding(build/'q4t', policy_file, policy_file.read_bytes())
    package.require(binding['backend'] == 'mwmbl_v2' and policy['scopes'] == ['client:alpha']
                    and policy['refresh_seconds'] == 0, 'expected a scoped qualification search policy')
    selected = {}
    for label in ('node-6.34.0','node-7.47.0'):
        directory = Path(sdk[label]).resolve(strict=True)
        metadata = package.read(directory/'package.json')
        package.require(metadata['name'] == 'openai' and label == 'node-'+metadata['version'],
                        'wrong stream SDK distribution')
        selected[label] = str(directory)
    lifecycle.prepare_from_manifest(out, build, deployment, proof['predecessor_manifest'],
                                    reuse.WEB_PROVIDER_REVIEWED, KEY, INPUTS)
    for role in ('primary','light'):
        phase = out/role
        plan = package.read(phase/'plan.json')
        policy_path = phase/'web-policy.json'
        package.save(policy_path, policy)
        plan['command'].extend(['--web-policy-file',str(policy_path)])
        plan[KEY].update(sdk=selected,policy_sha256=package.sha(policy_path))
        store = plan['command'][plan['command'].index('--object-store-dir')+1]
        package.require(store == str(out/'state/application-objects') and
                        package.sha(plan['ingress']) == proof['ingress'], 'wrong shared store or ingress')
        package.save(phase/'plan.json',plan)


def observe(stage, label, params):
    tls = stage.plan['tls']['certificate']
    config = dict(base=f"https://127.0.0.1:{stage.plan['public_port']}/v1",
                  key=stage.keys['alpha'], ca=tls, request=params)
    process = subprocess.run(['node',ACTOR,stage.plan[KEY]['sdk'][label]],
                             input=json.dumps(config).encode(),capture_output=True,timeout=110,
                             env=dict(os.environ,NODE_EXTRA_CA_CERTS=tls))
    (stage.out/(label+'-stream.stderr')).write_bytes(process.stderr)
    package.require(process.returncode == 0,'stream SDK failed for '+label)
    result = json.loads(process.stdout)
    package.require(result['sdk'] == label,'unexpected stream SDK')
    return result


def check(row, source, original=None):
    primary = original is None
    labels = ['node-6.34.0'] if primary else ['node-6.34.0','node-7.47.0']
    package.require(row['role'] == ('primary' if primary else 'light') and
                    sorted(row['readers']) == labels and row['response_id'].startswith('resp_') and
                    row['foreign'] == row['unknown'] == [404,'object_not_found'],
                    'role, readers or customer isolation changed')
    previous = None if primary else original['readers'][labels[0]]
    traces = set()
    for label in labels:
        value = row['readers'][label]
        transport, = value['requests']
        message, = value['messages']
        search, = value['searches']
        event, = value['annotations']
        path = '/v1/responses' if primary else '/v1/responses/'+row['response_id']
        package.require(value['sdk'] == label and value['id'] == row['response_id'] and
                        value['status']=='completed' and transport['status']==200 and
                        transport['path']==path and transport['trace'] and
                        (transport['method'],transport['query'])==
                        (('POST','') if primary else ('GET','stream=true')),
                        'wrong SDK, response or actual HTTP operation')
        package.require(value['events'] and all(pair[0]==i for i,pair in enumerate(value['events']))
                        and value['events'][-1][1]=='response.completed' and
                        event['item_id']==message[0] and message[2]==[event['annotation']] and
                        value['text']==message[1], 'missing text, citation or original event sequence')
        Witness.answer(value['text'],message[2],source['url'],source['title'])
        package.require(search[1]=='completed' and search[2]['queries']==['RFC 2606 reserved example domains'],
                        'unexpected search execution')
        if primary:
            package.require([s['url'] for s in search[2]['sources']]==[source['url']], 'unexpected sources')
        else:
            old = previous['searches'][0]
            package.require(all(value[k]==previous[k] for k in ('id','text','messages','events','annotations')) and
                            old[:2]==search[:2] and {k:v for k,v in old[2].items() if k!='sources'}==search[2],
                            'original event, search or source changed when read by another capacity')
        traces.add(transport['trace'])
    package.require(len(traces)==len(labels),'duplicate request trace')


def business(stage):
    role = stage.plan['name']
    source = package.read(PROOF)['question_source']
    row = dict(role=role,readers={})
    prior = None
    if role=='primary':
        body = package.read(QUESTION)
        body.update(store=True,max_output_tokens=stage.plan['capacity']['max_tokens'])
        value = observe(stage,'node-6.34.0',body)
        row['response_id'] = value['id']
        row['readers']['node-6.34.0'] = value
    else:
        prior = package.read(stage.out.parent/'primary'/RECEIPT)
        row['response_id'] = prior['response_id']
        for label in ('node-6.34.0','node-7.47.0'):
            row['readers'][label] = observe(stage,label,dict(response_id=row['response_id']))
    for key,response_id in [('foreign',row['response_id']),('unknown','resp_'+'0'*32)]:
        code, data = stage.api('GET','/v1/responses/'+response_id,client='beta')
        row[key]=[code,data.get('error',{}).get('code')]
    package.save(stage.out/RECEIPT,row)
    check(row,source,prior)
    stage.same_model()


def verify(phase, plan, capabilities, current):
    prior = profile_record.read(phase/'previous-profile.json')
    proof = package.read(PROOF)
    fields = plan[KEY]
    desired = copy.deepcopy(prior['capabilities'])
    desired['protocol_source_sha256'] = fields['protocol']
    package.require(package.same_json(desired, capabilities) and
                    plan['capacity'] == prior['capacity'] and
                    package.sha(phase/'previous-profile.json') == fields['previous'] == reuse.WEB_PROVIDER_REVIEWED[plan['name']] and
                    plan['binary_sha256']==proof['engine'] and plan['ingress_sha256']==proof['ingress'] and
                    fields['release']==plan['selected_sha256']==proof['predecessor_manifest'] and
                    package.sha(phase/'web-policy.json')==fields['policy_sha256'] and
                    all(plan['sources'][str(f.relative_to(ROOT))]==package.sha(f) for f in INPUTS),
                    'capacity, predecessor, engine, ingress or qualification sources changed')
    original = package.read(phase.parent/'primary'/RECEIPT) if plan['name']=='light' else None
    check(package.read(phase/RECEIPT),proof['question_source'],original)
    prev = prior['business']['live']['evidence_sha256']
    package.require(reuse.WEB_PROVIDER_RECEIPT in prev,'missing qualified web provider')
    return {**{k:v for k,v in prev.items() if k not in current},RECEIPT:package.sha(phase/RECEIPT)}

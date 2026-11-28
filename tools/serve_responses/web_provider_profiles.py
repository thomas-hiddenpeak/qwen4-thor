"""Qualify live search and cross-capacity offline retrieval on real clients."""
import copy
import json
from pathlib import Path

from tools.serve_recovery import profile_live_reuse as reuse, profile_record
from tools.serve_service import package as pkg
from tools.serve_responses.web_provider_acceptance import Witness

ROOT = Path(__file__).resolve().parents[2]
KEY = 'web_provider_handoff'
RECEIPT = 'web-provider-handoff.json'
EVIDENCE = ROOT/'docs/evidence/serve-web-provider-20261128'
INPUTS = (EVIDENCE/'device.json', EVIDENCE/'responses.request.json',
          Path(__file__), Path(__file__).with_name('web_provider_acceptance.py'))


def prepare(out, build, deployment, policy_file):
    from tools.serve_responses import environment_lifecycle_profiles as lifecycle
    proof = pkg.read(INPUTS[0])
    pkg.require(proof['engine'] == pkg.sha(build/'q4t') and
                proof['protocol_sources'] == pkg.sha(build/'generated/q4t/server/chat_protocol_sources.json')
                and proof['guard']['status'] == 'COMPLETED', 'unqualified web candidate')
    policy = pkg.read(policy_file)
    pkg.require(policy['scopes'] == ['client:alpha'] and policy['refresh_seconds'] == 0,
                'expected dedicated live qualification scope and forced refresh')
    binding = pkg.web_policy_binding(build/'q4t', policy_file, policy_file.read_bytes())
    pkg.require(binding['backend'] == 'mwmbl_v2', 'wrong discovery backend')
    lifecycle.prepare_from_manifest(out, build, deployment, proof['predecessor_manifest'],
                                    reuse.BATCH_CANCEL_REVIEWED, KEY, INPUTS)
    stores = set()
    for role in ('primary','light'):
        phase = out/role
        plan = pkg.read(phase/'plan.json')
        path = phase/'web-policy.json'
        pkg.save(path, policy)
        plan['command'].extend(['--web-policy-file', str(path)])
        plan[KEY]['policy_sha256'] = pkg.sha(path)
        stores.add(plan['command'][plan['command'].index('--object-store-dir') + 1])
        pkg.save(phase/'plan.json', plan)
    pkg.require(stores == {str(out/'state/application-objects')}, 'missing shared application store')


def check_answer(response, source):
    calls = [i for i in response.get('output',[]) if i['type']=='web_search_call']
    parts = [p for i in response.get('output',[]) if i['type']=='message'
             for p in i['content'] if p['type']=='output_text']
    pkg.require(response.get('status')=='completed' and len(calls)==len(parts)==1 and
                calls[0]['status']=='completed' and calls[0]['action']['queries']==['RFC 2606 reserved example domains'] and
                [s['url'] for s in calls[0]['action']['sources']]==[source['url']],
                'expected one verified RFC search and one generated answer')
    Witness.answer(parts[0]['text'], parts[0]['annotations'], source['url'], source['title'])


def check(role, row, source, original=None):
    pkg.require(row['role']==role and row['foreign_status']==404 and
                row['missing_status']==404 and row['foreign_error']==row['missing_error'] and
                bool(row['foreign_error']['type']) and row['request']['store'] is True and
                row['request']['tools'][0].get('external_web_access', True) is (role=='primary'),
                'scope isolation or live/offline mode changed')
    check_answer(row['response'],source)
    pkg.require(row['response']['id']==row['created_id'] and row['created_id'].startswith('resp_'),
                'created response identity differs')
    if role=='light':
        pkg.require(original is not None and row['created_id']!=original['created_id'] and
                    row['retrieved_id']==original['created_id'] and
                    pkg.same_json(row['retrieved']['output'],original['response']['output']),
                    'original stored search or response output changed across capacities')


def business(stage):
    role = stage.plan['name']
    proof = pkg.read(INPUTS[0])
    pkg.require(pkg.sha(stage.out/'web-policy.json')==stage.plan[KEY]['policy_sha256'],
                'qualification search policy changed')
    original = pkg.read(stage.out.parent/'primary'/RECEIPT) if role=='light' else None
    body = pkg.read(INPUTS[1])
    body.update(max_output_tokens=stage.plan['capacity']['max_tokens'],store=True)
    if role=='light':
        body['tools'][0]['external_web_access'] = False
    status, response = stage.api('POST','/v1/responses',body=body,timeout=110)
    pkg.save(stage.out/'web-create.json',dict(status=status,response=response))
    pkg.require(status==200,'search returned '+str(status))
    row = dict(role=role,request=body,response=response,created_id=response['id'])
    target = original['created_id'] if original else row['created_id']
    url = '/v1/responses/'+target+'?include%5B%5D=web_search_call.action.sources'
    if original:
        status, data = stage.api('GET',url)
        pkg.require(status==200,'original response no longer available')
        row.update(retrieved_id=data['id'],retrieved=data)
    code, foreign = stage.api('GET',url,client='beta')
    missing_code, missing = stage.api('GET','/v1/responses/resp_'+'0'*32,client='beta')
    def error(value):
        return {key:value['error'].get(key) for key in ('type','code','param')}
    row.update(foreign_status=code,foreign_error=error(foreign),
               missing_status=missing_code,missing_error=error(missing))
    pkg.save(stage.out/RECEIPT,row)
    check(role,row,proof['source'],original)
    stage.same_model()


def verify(phase, plan, capabilities, current):
    role = plan['name']
    prior = profile_record.read(phase/'previous-profile.json')
    change = plan[KEY]
    proof = pkg.read(INPUTS[0])
    desired = copy.deepcopy(prior['capabilities'])
    desired['protocol_source_sha256'] = change['protocol']
    for item in (desired['web_search'],desired['responses']['web_search']):
        item['optional_providers'] = {'mwmbl_v2': True}
    pkg.require(pkg.same_json(desired,capabilities) and plan['capacity']==prior['capacity']
                and pkg.sha(phase/'previous-profile.json')==change['previous']==reuse.BATCH_CANCEL_REVIEWED[role]
                and proof['engine']==plan['binary_sha256'] and
                change['release']==plan['selected_sha256']==proof['predecessor_manifest'] and
                pkg.sha(phase/'web-policy.json')==change['policy_sha256'] and
                all(plan['sources'][str(p.relative_to(ROOT))]==pkg.sha(p) for p in INPUTS),
                'source, capacity or provider declaration changed')
    check(role,pkg.read(phase/RECEIPT),proof['source'],
          pkg.read(phase.parent/'primary'/RECEIPT) if role=='light' else None)
    prior_files = prior['business']['live']['evidence_sha256']
    pkg.require(reuse.BATCH_CANCEL_RECEIPT in prior_files,'missing previous qualified record')
    return {**{k:v for k,v in prior_files.items() if k not in current},RECEIPT:pkg.sha(phase/RECEIPT)}

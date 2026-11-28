"""Carry the same cancelled Batch and completed project row from primary to light."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

from tools.serve_audit.batch_cancel_receipt import verify as verify_pair
from tools.serve_recovery import profile_live_reuse as reuse, profile_record
from tools.serve_service import package

ROOT = Path(__file__).resolve().parents[2]
KEY = 'batch_cancellation_handoff'
RECEIPT = 'batch-cancellation-handoff.json'
PROOF = ROOT/'docs/evidence/serve-batch-cancel-20261128/device.json'
DOCUMENT = PROOF.with_name('project-release.json')
ACTORS = {name: ROOT/'tools/serve_audit'/name for name in (
    'concurrent_batch_business.py','batch_checkpoint.py','batch_handoff.mjs','batch_cancel_receipt.py')}


def prepare(out, build, deployment, python, node):
    from tools.serve_responses import environment_lifecycle_profiles as lifecycle
    proof = package.read(PROOF)
    package.require(proof['engine']==package.sha(build/'q4t') and proof['protocol_sources']==
                    package.sha(build/'generated/q4t/server/chat_protocol_sources.json') and
                    proof['document_sha256']==package.sha(DOCUMENT) and proof['callers']==4 and
                    proof['guard']['status']=='COMPLETED', 'missing qualified cancellation build')
    lifecycle.prepare_from_manifest(out,build,deployment,proof['predecessor_manifest'],
        reuse.DIAGNOSTIC_OUTPUT_REVIEWED,KEY,[PROOF,DOCUMENT,Path(__file__),*ACTORS.values()])
    issued = package.read(deployment/'build/service/engine/client-policy.json')
    issued_owner = next(c for c in issued['clients'] if c['id']=='alpha')
    package.require(issued_owner['max_inflight'] == 2, 'issued Batch owner capacity changed')
    object_roots = set()
    for name in ('primary','light'):
        phase=out/name
        plan=package.read(phase/'plan.json')
        plan[KEY].update(python=str(python),node=str(node),callers=2,requests=55)
        policy = package.read(phase/'client-policy.json')
        client = next(v for v in policy['clients'] if v['id']=='alpha')
        package.require(client['max_control_inflight'] >= 2, 'capacity lacks two control slots')
        if name == 'primary':
            # A running Batch uses one data permit while the SDK uploads the second input.
            client['max_inflight'] = issued_owner['max_inflight']
            package.save(phase/'client-policy.json', policy)
            shutil.copy2(phase/'client-policy.json', phase/'engine/client-policy.json')
            plan.update(client_policy_sha256=package.sha(phase/'client-policy.json'),
                        installed_policy_sha256=package.sha(phase/'engine/client-policy.json'))
        command = plan['command']
        object_roots.add(command[command.index('--object-store-dir') + 1])
        package.save(phase/'plan.json',plan)
    package.require(object_roots == {str(out/'state/application-objects')}, 'shared private store differs')


def check_source(writer, settled, document):
    require=package.require
    for name, label, status in [('completed','finished','completed'),('pending','stopped','cancelled')]:
        expected, observed = writer[name], settled[label]
        require(observed['id']==expected['id'] and observed['input_file_id']==expected['input'] and
                observed['status']==status and observed['metadata']==expected['metadata'],'original Batch differs')
    require(len(writer['cancel'])==2 and all(c['id']==settled['stopped']['id'] and
            c['cancelling_at']==settled['stopped']['cancelling_at'] for c in writer['cancel']),
            'missing original concurrent cancellation')
    require(writer['revision']==json.loads(document)['publication_revision'] and
            settled['sdk']==writer['sdk']=='python-3.55.1' and settled['output_bytes']>0 and
            len(settled['output_sha256'])==64 and settled['row_id'],'original project output missing')
    requests=settled['requests']
    counts={name:0 for name in ('finished','stopped')}
    output='/v1/files/'+settled['finished']['output_file_id']+'/content'
    for req in requests[:-1]:
        matches=[label for label in counts if req['path']=='/v1/batches/'+settled[label]['id']]
        require(len(matches)==1,'unexpected Batch checkpoint lookup')
        counts[matches[0]]+=1
    require(all(1<=n<=15 for n in counts.values()) and requests[-1]['path']==output and
            all(r['method']=='GET' and r['status']==200 and not r['query'] for r in requests),
            'terminal read or original output missing')
    traces=[r['trace'] for r in requests+writer['requests']]
    require(all(isinstance(v,str) and v.startswith('req-') for v in traces) and
            len(set(traces))==len(traces),'missing or repeated checkpoint request')


def check_handoff(first, second, document, model):
    check_source(first['writer'],first['settled'],document)
    reader,old=second['reader'],first['settled']
    ids=verify_pair(first['writer'],reader,document,model,callers=2)
    for key in ('stopped','finished'):
        package.require(package.same_json(old[key],reader[key]),'original terminal Batch changed')
    raw=reader['output_text'].encode()
    package.require(len(raw)==old['output_bytes'] and hashlib.sha256(raw).hexdigest()==old['output_sha256']
                    and reader['result']['id']==old['row_id'],'original result file changed')
    package.require(ids.isdisjoint(x['trace'] for x in old['requests']),'cross-profile trace reused')


def business(stage):
    stage.same_model()
    plan=stage.plan[KEY]
    cfg=dict(base=f"https://127.0.0.1:{stage.plan['public_port']}/v1",key=stage.keys['alpha'],peer=stage.keys['beta'],
             certificate=stage.plan['tls']['certificate'],model='qwen3.8-flash-next',document=DOCUMENT.read_text())
    def run(name,cmd,inputs):
        capture = stage.out/(name+'-requests.json')
        proc=subprocess.run(list(map(str,cmd)),input=json.dumps(dict(inputs,capture=str(capture))).encode(),capture_output=True,timeout=120,
                            env=dict(os.environ,NODE_EXTRA_CA_CERTS=cfg['certificate']))
        error=proc.stderr.decode(errors='replace').replace(cfg['key'],'<redacted>').replace(cfg['peer'],'<redacted>')
        (stage.out/(name+'.stderr')).write_text(error)
        package.require(proc.returncode==0,'Batch profile actor failed: '+name)
        record = json.loads(proc.stdout)
        package.require(record['requests'] == package.read(capture), 'original SDK request log differs')
        return record
    if stage.plan['name']=='primary':
        writer=run('batch-submit',[plan['python'],'-I','-B',ACTORS['concurrent_batch_business.py']],dict(cfg,callers=2))
        settled=run('batch-checkpoint',[plan['python'],'-I','-B',ACTORS['batch_checkpoint.py']],dict(cfg,writer=writer))
        result=dict(writer=writer,settled=settled)
        check_source(writer,settled,cfg['document'])
    else:
        first=package.read(stage.out.parent/'primary'/RECEIPT)
        reader=run('batch-read',['node',ACTORS['batch_handoff.mjs'],plan['node']],
                   dict(cfg,writer=first['writer'],max_requests=plan['requests']))
        result=dict(reader=reader)
        check_handoff(first,result,cfg['document'],cfg['model'])
    stage.same_model()
    package.save(stage.out/RECEIPT,result)


def verify(phase, plan, capabilities, current):
    previous_file=phase/'previous-profile.json'
    previous=profile_record.read(previous_file)
    change=plan[KEY];name=plan['name'];proof=package.read(PROOF)
    package.require(package.sha(previous_file)==change['previous']==reuse.DIAGNOSTIC_OUTPUT_REVIEWED[name] and
                    proof['predecessor_manifest']==change['release']==plan['selected_sha256'] and
                    proof['engine']==plan['binary_sha256'] and plan['capacity']==previous['capacity'] and
                    package.same_json(capabilities,dict(previous['capabilities'],protocol_source_sha256=change['protocol'])),
                    'Batch profile, tested binary or effective capabilities differ')
    for path in (PROOF,DOCUMENT,Path(__file__),*ACTORS.values()):
        package.require(plan['sources'][str(path.relative_to(ROOT))] == package.sha(path),
                        'Batch actor or original release record changed')
    row=package.read(phase/RECEIPT)
    if name=='primary':
        check_source(row['writer'],row['settled'],DOCUMENT.read_text())
    else:
        check_handoff(package.read(phase.parent/'primary'/RECEIPT),row,DOCUMENT.read_text(),proof['model'])
    inherited=previous['business']['live']['evidence_sha256']
    package.require(reuse.DIAGNOSTIC_OUTPUT_RECEIPT in inherited,'missing issued diagnostics qualification')
    return {**{k:v for k,v in inherited.items() if k not in current},RECEIPT:package.sha(phase/RECEIPT)}

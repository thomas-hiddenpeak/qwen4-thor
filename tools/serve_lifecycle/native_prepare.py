"""Freeze one N07 capacity/lifecycle business run; no GPU calls or default edits."""
import copy
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys

sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.serve_lifecycle import native_acceptance as a
from tools.serve_progress.prepare import copy_file,requests
from tools.serve_recovery import bundle
from tools.serve_service import package as p,capacity

BASE=ROOT/'.q4t-work/serve-lifecycle-20261109'
BUILD=ROOT/'.q4t-work/serve-custom-tools-20261108/engine-build'
OUT=BASE/'business-01'
CAPACITY=dict(version=1,max_len=4096,max_prefill=1024,max_tokens=128,
              max_inflight_requests=6,max_queued_requests=4,request_deadline_ms=180000)


def main():
    os.umask(0o077)
    p.require(not OUT.exists(),'N07 output exists')
    p.require(not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT),
              'commit frozen N07 inputs first')
    p.require(p.read(BASE/'native-driver-cpu-01/result.json')['status']=='PASS','N07 driver CPU gate')
    build=p.read(BASE/'business-build-01/result.json')
    p.require(build['exit_code']==0 and build['warnings']==0,'N07 build gate')
    runtime=p.read(BASE/'business-build-01/runtime-source.json')
    p.require(all(p.sha(ROOT/n)==h for n,h in runtime.items()),'N07 runtime source drift')
    native=p.read(BASE/'business-build-01/native-build.json')
    p.require(all(p.sha(ROOT/n)==h for n,h in native['source_files'].items()),'N07 native source drift')
    for name,key in [('q4t-ingress','ingress_sha256'),('q4t-guardian','guardian_sha256'),
                     ('q4t-cuda-probe','cuda_probe_sha256')]:
        p.require(p.sha(BUILD/name)==native[key],'N07 native binary drift')
    old=(ROOT/'build/service').resolve(strict=True);bundle.verify(old,p.sha(old/'manifest.json'))
    sources=p.read(BUILD/'generated/q4t/server/chat_protocol_sources.json')
    p.require(all(p.sha(ROOT/n)==h for n,h in sources.items()),'N07 embedded protocol drift')
    protocol=hashlib.sha256(''.join(n+':'+h+'\n' for n,h in sorted(sources.items())).encode()).hexdigest()
    for port in (18971,18972,18973):
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);sock.bind(('127.0.0.1',port))
    OUT.mkdir(mode=0o700);p.save(OUT/'native-build.json',native)
    policy=p.read(old/'gateway-policy.json')
    policy.update(listen_port=18971,upstream_port=18972,upstream_operations_port=18973)
    policy['clients']=[dict(c,api_key='n07-test-'+secrets.token_hex(24)) for c in policy['clients']]
    backend=p.read(old/'engine/client-policy.json');backend['gateway_token']=secrets.token_hex(32)
    p.save(OUT/'gateway-policy.json',policy);p.save(OUT/'client-policy.json',backend)
    for name in ('q4t','CMakeCache.txt'):copy_file(BUILD/name,OUT/'candidate'/name)
    declarations=OUT/'candidate/declarations';declarations.mkdir()
    for name in p.declaration_files(p.APPLICATION_DECLARATION_PROFILE):
        source=old/'engine/declarations'/name
        if name in ('capabilities.json','configuration.json'):
            value=p.read(source);value['protocol_source_sha256']=protocol;p.save(declarations/name,value)
        else:copy_file(source,declarations/name)
    caps=p.read(declarations/'capabilities.json');metadata=p.read(ROOT/'build/q4t.release.json')
    decision=dict(status='PENDING_REAL_BUSINESS_N07',validation_only=True,limits=a.LIMITS,
        binary_sha256=p.sha(OUT/'candidate/q4t'),protocol_sha256=protocol,
        declaration_basis='Expected unchanged protocol plus CLI dispatch source identity. '
            'N07 actual capacity discovery and business still required.',actual_observations_required=True)
    p.save(OUT/'pending-decision.json',decision)
    metadata.update(validation_only=True,acceptance='VALIDATION_ONLY_N07',version='n07-validation-only',
        binary_sha256=decision['binary_sha256'],source_commit=subprocess.check_output(
            ['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        build_cache=str(OUT/'candidate/CMakeCache.txt'),build_cache_sha256=p.sha(OUT/'candidate/CMakeCache.txt'),
        acceptance_decision=str(OUT/'pending-decision.json'),acceptance_decision_sha256=p.sha(OUT/'pending-decision.json'),
        observed_configuration=p.read(declarations/'configuration.json'),validation_declaration_basis=decision['declaration_basis'])
    metadata['chat_protocol'].update(caps,protocol_sha256=protocol)
    for name in p.declaration_schemas(p.APPLICATION_DECLARATION_PROFILE):
        path=declarations/'schemas'/(name+'.json');metadata['chat_protocol'][name+'_schema']=str(path)
        metadata['chat_protocol'][name+'_schema_sha256']=p.sha(path)
    metadata['release_declarations']=dict(profile=p.APPLICATION_DECLARATION_PROFILE,directory=str(declarations),
        binary_sha256=metadata['binary_sha256'],protocol_sha256=protocol,
        files={n:p.sha(declarations/n) for n in p.declaration_files(p.APPLICATION_DECLARATION_PROFILE)})
    p.save(OUT/'candidate/q4t.release.json',metadata)
    expected=dict(metadata=a.fingerprint(metadata),decision=a.fingerprint(decision))
    try:p.accepted_release(metadata,decision)
    except RuntimeError:pass
    else:raise RuntimeError('production gate accepted pending N07')
    p.accepted_release=a.validation_gate(p.accepted_release,expected)
    (OUT/'objects').mkdir(mode=0o700);p.save(OUT/'capacity.json',CAPACITY)
    receipt=bundle.prepare(OUT/'candidate/q4t',OUT/'candidate/q4t.release.json',p.read(old/'engine/manifest.json')['model_dir'],
        BUILD/'q4t-guardian',BUILD/'q4t-cuda-probe',None,OUT/'gateway-policy.json',OUT/'client-policy.json',OUT/'bundle',
        native_receipt=OUT/'native-build.json',native_ingress=BUILD/'q4t-ingress',capacity_config=CAPACITY,
        application_state=dict(directory=str(OUT/'objects')))
    p.save(OUT/'receipt.json',receipt)
    templates=requests();chosen={label:copy.deepcopy(templates[source]) for label,source in
        [('json','initial-json'),('sse','initial-sse'),('tool','tool'),('follow','follow')]}
    for name,value in chosen.items():value['request_id']='n07-'+name
    paths=set(sources)|set(runtime)|set(bundle.SOURCES)|set(bundle.NATIVE_SOURCES)|{
        'tools/serve_lifecycle/native_acceptance.py','tools/serve_lifecycle/native_prepare.py',
        'tools/serve_lifecycle/test_native_acceptance.py','tools/serve_recovery/acceptance.py',
        'tools/serve_progress/prepare.py','tools/examples/chat_client.py',
        'tools/examples/project_records.py','docs/MODEL.md'}
    protected=[ROOT/'build'/n for n in ('q4t','q4t.release.json','service.release.json',
        'service-native-state/service.json','service-native-state/ledger.json')]
    protected += [old/n for n in ('manifest.json','gateway-policy.json','engine/client-policy.json')]
    protected += list((ROOT/'build/service-native-state').glob('client-*/policy.json'))
    plan=dict(version=1,scenario='native_service_lifecycle',output=str(OUT),package=receipt,
        source_commit=metadata['source_commit'],sources={n:p.sha(ROOT/n) for n in sorted(paths)},
        protected=[dict(path=str(n),sha256=p.sha(n)) for n in protected],validation_release=expected,
        protocol_sha256=protocol,limits=a.LIMITS,capacity_profile=capacity.normalize(CAPACITY),
        requests=chosen,sequence=['CLI handler run/self-check','actual custom capacity discovery',
            'CLI check/status','JSON and SSE arithmetic','actual project tool/citation',
            'CLI live limits reload with same model','CLI stop/durable cleanup'],
        default_replaced=False,performance_acceptance=False,cpp_dispatch_validated_on_cpu=True)
    p.save(OUT/'plan.json',plan)
    print(json.dumps(dict(plan=str(OUT/'plan.json'),package=receipt,limits=a.LIMITS,validation_only=True)))


if __name__=='__main__':main()

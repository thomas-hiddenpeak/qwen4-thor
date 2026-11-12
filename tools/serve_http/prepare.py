"""Seal N05 validation package and frozen budget without GPU/model execution."""
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
from tools.serve_http import acceptance as a
from tools.serve_progress.prepare import copy_file,requests
from tools.serve_protocol.stage import sdk_identity
from tools.serve_recovery import bundle
from tools.serve_recovery.test_native_access import certificates,CORS
from tools.serve_service import package as p
BASE=ROOT/'.q4t-work/serve-http-20261109'
BUILD=ROOT/'.q4t-work/serve-custom-tools-20261108/engine-build'
OUT=BASE/'business-01'


def expected_capabilities(value,protocol):
    value=copy.deepcopy(value);value['protocol_source_sha256']=protocol
    value['http'].update(connection_reuse=True,keep_alive_max_requests=100,
        keep_alive_idle_ms=5000,chunked_requests=True,
        expect_continue='after_authentication_and_admission',sse_http11_framing='chunked',
        request_identity='reauthenticate_each_request',truncated_stream='close_without_replay')
    value['observability']['transport_counts']='first_receive_and_write_failure_per_request'
    return value


def main():
    os.umask(0o077)
    p.require(not OUT.exists(),'N05 output exists')
    p.require(not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT),'commit N05 sources first')
    p.require(p.read(BASE/'driver-cpu-01/result.json')['exit_code']==0,'N05 driver CPU gate')
    for group in ('serve-http-framing-20261109','serve-http-session-20261109',
                  'serve-http-proxy-20261109','serve-http-package-20261109','serve-http-driver-20261109'):
        directory=ROOT/'docs/evidence'/group
        p.require(all(p.sha(directory/n)==row['sha256'] for n,row in p.read(directory/'index.json').items()),
                  'N05 prerequisite evidence drift')
    build=p.read(BASE/'discovery-build-01/result.json')
    p.require(build['exit_code']==0 and build['warnings']==0,'N05 build gate')
    runtime=p.read(BASE/'discovery-build-01/runtime-source.json')
    p.require(all(p.sha(ROOT/n)==h for n,h in runtime.items()),'runtime changed since build')
    native=p.read(BASE/'final-build-01/native-build.json')
    p.require(all(p.sha(ROOT/n)==h for n,h in native['source_files'].items()),'native source drift')
    for name,field in [('q4t-ingress','ingress_sha256'),('q4t-guardian','guardian_sha256'),('q4t-cuda-probe','cuda_probe_sha256')]:
        p.require(p.sha(BUILD/name)==native[field],'native binary drift')
    old=(ROOT/'build/service').resolve(strict=True);bundle.verify(old,p.sha(old/'manifest.json'))
    sources=p.read(BUILD/'generated/q4t/server/chat_protocol_sources.json')
    p.require(all(p.sha(ROOT/n)==h for n,h in sources.items()),'compiled protocol source drift')
    protocol=hashlib.sha256(''.join(n+':'+h+'\n' for n,h in sorted(sources.items())).encode()).hexdigest()
    for port in (18871,18872,18873):
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);sock.bind(('127.0.0.1',port))
    OUT.mkdir(mode=0o700);certificates(OUT/'tls');p.save(OUT/'native-build.json',native)
    policy=p.read(old/'gateway-policy.json')
    policy.update(version=4,listen_host='127.0.0.1',server_name='127.0.0.1',listen_port=18871,
        upstream_port=18872,upstream_operations_port=18873,cors=copy.deepcopy(CORS),
        allowed_client_cidrs=['127.0.0.1/32','192.0.2.9/32'],http=dict(path_prefix='/llm/thor',
            keep_alive_requests=20,keep_alive_idle_seconds=5,request_timeout_seconds=10,
            proxy=dict(trusted_cidrs=['127.0.0.1/32'],header='forwarded',allow_stripped_prefix=True)))
    policy['clients']=[dict(c,api_key='n05-test-'+secrets.token_hex(24)) for c in policy['clients']]
    backend=p.read(old/'engine/client-policy.json')
    backend['clients']=[dict(c,requests_per_minute=0,tokens_per_minute=0) for c in backend['clients']]
    backend['gateway_token']=secrets.token_hex(32)
    p.save(OUT/'gateway-policy.json',policy);p.save(OUT/'client-policy.json',backend)
    for name in ('q4t','CMakeCache.txt'):copy_file(BUILD/name,OUT/'candidate'/name)
    declarations=OUT/'candidate/declarations';declarations.mkdir()
    for name in p.declaration_files(p.APPLICATION_DECLARATION_PROFILE):
        source=old/'engine/declarations'/name
        if name=='capabilities.json':p.save(declarations/name,expected_capabilities(p.read(source),protocol))
        elif name=='configuration.json':
            value=p.read(source);value['protocol_source_sha256']=protocol;p.save(declarations/name,value)
        else:copy_file(source,declarations/name)
    caps=p.read(declarations/'capabilities.json');metadata=p.read(ROOT/'build/q4t.release.json')
    decision=dict(status='PENDING_REAL_BUSINESS_N05',validation_only=True,limits=a.LIMITS,
        binary_sha256=p.sha(OUT/'candidate/q4t'),protocol_sha256=protocol,
        declaration_basis='Expected from accepted N04 plus compiled HTTP capabilities; actual discovery required.',
        actual_observations_required=True)
    p.save(OUT/'pending-decision.json',decision)
    metadata.update(validation_only=True,acceptance='VALIDATION_ONLY_N05',version='n05-validation-only',
        binary_sha256=decision['binary_sha256'],source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
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
    else:raise RuntimeError('production accepted pending N05 candidate')
    p.accepted_release=a.validation_gate(p.accepted_release,expected)
    (OUT/'objects').mkdir(mode=0o700)
    receipt=bundle.prepare(OUT/'candidate/q4t',OUT/'candidate/q4t.release.json',p.read(old/'engine/manifest.json')['model_dir'],
        BUILD/'q4t-guardian',BUILD/'q4t-cuda-probe',None,OUT/'gateway-policy.json',OUT/'client-policy.json',OUT/'bundle',
        native_receipt=OUT/'native-build.json',native_ingress=BUILD/'q4t-ingress',
        certificate=OUT/'tls/server.pem',key=OUT/'tls/server.key',application_state=dict(directory=str(OUT/'objects')))
    p.save(OUT/'receipt.json',receipt)
    sdk=sdk_identity();templates=requests()
    for name in ('tool','follow'):templates[name]['request_id']='n05-'+name
    paths=set(sources)|set(runtime)|set(bundle.SOURCES)|set(bundle.NATIVE_SOURCES)|{
        'tools/serve_http/acceptance.py','tools/serve_http/prepare.py','tools/serve_http/test_acceptance.py',
        'tools/serve_access/acceptance.py','tools/serve_progress/prepare.py','tools/serve_recovery/acceptance.py',
        'tools/serve_recovery/test_native_access.py','tools/examples/chat_client.py','tools/examples/project_records.py',
        'tools/serve_protocol/js_client.py','tools/serve_protocol/run_sdk.mjs','tools/serve_protocol/sdk.mjs',
        'tools/examples/js/chat_client.mjs','tools/examples/js/project_profile.mjs',
        'docs/MODEL.md','docs/serve-config-v1.schema.json'}
    protected=[ROOT/'build'/n for n in ('q4t','q4t.release.json','service.release.json','service-native-state/service.json','service-native-state/ledger.json')]
    protected += [old/n for n in ('manifest.json','gateway-policy.json','engine/client-policy.json')]
    plan=dict(version=1,scenario='native_http_transport',output=str(OUT),package=receipt,
        source_commit=metadata['source_commit'],sources={n:p.sha(ROOT/n) for n in sorted(paths)},
        protected=[dict(path=str(n),sha256=p.sha(n)) for n in protected],validation_release=expected,
        protocol_sha256=protocol,limits=a.LIMITS,sdk=sdk,
        tls_inputs={name:dict(path=str(path),sha256=p.sha(path)) for name,path in
            [('ca',OUT/'tls/ca.pem'),('certificate',OUT/'tls/server.pem'),('key',OUT/'tls/server.key')]},
        requests={n:templates[n] for n in ('tool','follow')},
        sequence=['managed self-check','direct core chunked/100/reuse','native TLS JSON/SSE/reuse/reauth',
            'official SDK SSE','real read-only project tool closure','Responses/Completions reuse',
            'actual optional proxy with prefix stripping','same model identity and drained ownership','durable stop/cleanup'],
        default_replaced=False,performance_acceptance=False)
    p.save(OUT/'plan.json',plan)
    print(json.dumps(dict(plan=str(OUT/'plan.json'),package=receipt,limits=a.LIMITS,validation_only=True)))


if __name__=='__main__':main()

"""Seal N04 validation-only native TLS bundles, never an accepted default."""
import copy
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import uuid

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_access import acceptance as a
from tools.serve_progress.prepare import copy_file, requests
from tools.serve_protocol.stage import sdk_identity
from tools.serve_recovery import bundle
from tools.serve_recovery.test_native_access import certificates, CORS
from tools.serve_service import package as p

BASE = ROOT/'.q4t-work/serve-access-20261109'
BUILD = ROOT/'.q4t-work/serve-custom-tools-20261108/engine-build'
OUT = BASE/'business-01'


def main():
    os.umask(0o077)
    p.require(not OUT.exists(), 'N04 output already exists')
    p.require(not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT),
              'commit reviewed N04 driver before sealing sources')
    cpu = p.read(BASE/'driver-cpu-01/result.json')
    p.require(cpu['exit_code'] == 0 and cpu['count'] == 4, 'N04 driver CPU gate')
    for group in ('serve-initialization-20261109', 'serve-deployment-20261109', 'serve-usage-runtime-20261109'):
        source = ROOT/'docs/evidence'/group
        p.require(all(p.sha(source/n) == row['sha256'] for n,row in p.read(source/'index.json').items()),
                  'N04 prerequisite evidence changed')
    p.require(p.read(BASE/'native-build-02/result.json')['exit_code'] == 0 and
              p.read(BASE/'native-build-02/result.json')['warnings'] == 0, 'N04 native build')
    p.require(p.sha(BUILD/'q4t') == 'c344d22ff3befc4464259a020f69ab5ea83efbbf6916e1120ec47faa626b5ad3',
              'N04 CPU-validated engine changed')
    old = (ROOT/'build/service').resolve(strict=True)
    bundle.verify(old,p.sha(old/'manifest.json'))
    protocol_sources = p.read(BUILD/'generated/q4t/server/chat_protocol_sources.json')
    p.require(all(p.sha(ROOT/n) == h for n,h in protocol_sources.items()), 'compiled source drift')
    protocol = hashlib.sha256(''.join(n+':'+h+'\n' for n,h in sorted(protocol_sources.items())).encode()).hexdigest()
    for port in (18771,18772,18773):
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);sock.bind(('127.0.0.1',port))
    OUT.mkdir(mode=0o700);certificates(OUT/'tls')
    native = p.read(BASE/'native-build-02/native-build.json')
    p.require(all(p.sha(ROOT/n) == h for n,h in native['source_files'].items()), 'native build source drift')
    p.save(OUT/'native-build.json',native)
    policy = p.read(old/'gateway-policy.json')
    policy.update(version=3,listen_host='127.0.0.1',server_name='127.0.0.1',listen_port=18771,
                  upstream_port=18772,upstream_operations_port=18773,cors=copy.deepcopy(CORS))
    policy['clients'] = [dict(policy['clients'][0],id='alpha',api_key='n04-initial-'+secrets.token_hex(24))]
    backend = p.read(old/'engine/client-policy.json')
    backend['clients'] = [dict(backend['clients'][0],id='alpha',requests_per_minute=0,tokens_per_minute=0)]
    backend['gateway_token'] = secrets.token_hex(32)
    p.save(OUT/'gateway-policy.json',policy);p.save(OUT/'client-policy.json',backend)
    for name in ('q4t','CMakeCache.txt'):copy_file(BUILD/name,OUT/'candidate'/name)
    declarations = OUT/'candidate/declarations';declarations.mkdir()
    for name in p.declaration_files(p.APPLICATION_DECLARATION_PROFILE):
        source = old/'engine/declarations'/name
        if name.endswith('.json') and not name.startswith('schemas/'):
            value = p.read(source);value['protocol_source_sha256'] = protocol;p.save(declarations/name,value)
        else:
            if name == 'schemas/config.json':source = ROOT/'docs/serve-config-v1.schema.json'
            copy_file(source,declarations/name)
    caps = p.read(declarations/'capabilities.json')
    metadata = p.read(ROOT/'build/q4t.release.json')
    decision = dict(status='PENDING_REAL_BUSINESS_N04',validation_only=True,limits=a.LIMITS,
        binary_sha256=p.sha(OUT/'candidate/q4t'),protocol_sha256=protocol,
        declaration_basis='Expected public declarations from accepted engine with compiled identity; '
            'candidate capabilities/configuration must be observed during real N04 business.',
        actual_observations_required=True)
    p.save(OUT/'pending-decision.json',decision)
    metadata.update(validation_only=True,acceptance='VALIDATION_ONLY_N04',version='n04-validation-only',
        binary_sha256=decision['binary_sha256'],
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        build_cache=str(OUT/'candidate/CMakeCache.txt'),build_cache_sha256=p.sha(OUT/'candidate/CMakeCache.txt'),
        acceptance_decision=str(OUT/'pending-decision.json'),acceptance_decision_sha256=p.sha(OUT/'pending-decision.json'),
        observed_configuration=p.read(declarations/'configuration.json'),
        validation_declaration_basis=decision['declaration_basis'])
    metadata['chat_protocol'].update(caps,protocol_sha256=protocol)
    for name in p.declaration_schemas(p.APPLICATION_DECLARATION_PROFILE):
        path = declarations/'schemas'/(name+'.json')
        metadata['chat_protocol'][name+'_schema'] = str(path)
        metadata['chat_protocol'][name+'_schema_sha256'] = p.sha(path)
    metadata['release_declarations'] = dict(profile=p.APPLICATION_DECLARATION_PROFILE,
        directory=str(declarations),binary_sha256=metadata['binary_sha256'],protocol_sha256=protocol,
        files={n:p.sha(declarations/n) for n in p.declaration_files(p.APPLICATION_DECLARATION_PROFILE)})
    p.save(OUT/'candidate/q4t.release.json',metadata)
    expected = dict(metadata=a.fingerprint(metadata),decision=a.fingerprint(decision))
    try:p.accepted_release(metadata,decision)
    except RuntimeError:pass
    else:raise RuntimeError('production verifier accepted pending N04 candidate')
    p.accepted_release = a.validation_gate(p.accepted_release,expected)
    (OUT/'objects').mkdir(mode=0o700)
    model = p.read(old/'engine/manifest.json')['model_dir']
    receipt = bundle.prepare(OUT/'candidate/q4t',OUT/'candidate/q4t.release.json',model,
        BUILD/'q4t-guardian',BUILD/'q4t-cuda-probe',None,OUT/'gateway-policy.json',OUT/'client-policy.json',
        OUT/'bundle',native_receipt=OUT/'native-build.json',native_ingress=BUILD/'q4t-ingress',
        certificate=OUT/'tls/server.pem',key=OUT/'tls/server.key',
        application_state=dict(directory=str(OUT/'objects')))
    p.save(OUT/'receipt.json',receipt)
    shutil.copytree(OUT/'bundle',OUT/'bundle-second')
    second = dict(receipt,package=str(OUT/'bundle-second'))
    bundle.verify(second['package'],second['manifest_sha256'])
    p.save(OUT/'second-receipt.json',second)
    sdk = sdk_identity()
    templates = requests()
    templates['tool']['request_id'] = 'n04-project-tool';templates['follow']['request_id'] = 'n04-project-final'
    sources = set(protocol_sources)|set(bundle.SOURCES)|set(bundle.NATIVE_SOURCES)|{
        'tools/serve_access/acceptance.py','tools/serve_access/prepare.py','tools/serve_access/test_acceptance.py',
        'tools/serve_recovery/acceptance.py','tools/serve_recovery/test_native_access.py',
        'tools/examples/chat_client.py','tools/examples/project_records.py','docs/MODEL.md',
        'tools/serve_protocol/js_client.py','tools/serve_protocol/run_sdk.mjs','tools/serve_protocol/sdk.mjs',
        'docs/serve-config-v1.schema.json'}
    protected = [ROOT/'build'/n for n in ('q4t','q4t.release.json','service.release.json',
        'service-native-state/service.json','service-native-state/ledger.json')]
    protected += [old/n for n in ('manifest.json','gateway-policy.json','engine/client-policy.json')]
    plan = dict(version=1,scenario='native_client_access',output=str(OUT),package=receipt,second_package=second,
        source_commit=metadata['source_commit'],sources={n:p.sha(ROOT/n) for n in sorted(sources)},
        protected=[dict(path=str(n),sha256=p.sha(n)) for n in protected],validation_release=expected,
        protocol_sha256=protocol,limits=a.LIMITS,sdk=sdk,
        tls_inputs={name:dict(path=str(path),sha256=p.sha(path)) for name,path in
            [('ca',OUT/'tls/ca.pem'),('certificate',OUT/'tls/server.pem'),('key',OUT/'tls/server.key')]},
        switch_id=str(uuid.uuid4()),rollback_id=str(uuid.uuid4()),requests={k:templates[k] for k in ('tool','follow')},
        validation_wrappers=['exact pending-release gate','count starts/self-check before dispatch',
            'read-only policy/usage snapshots','only dedicated state journal files made temporarily unavailable'],
        sequence=['TLS key overlap/revocation','actual document tool closure','new client and official SDK',
            'off-to-on RPM/TPM retains usage','limits I/O loss keeps core and key revocation',
            'stored Chat client isolation','sealed stop; upgrade preserves exact usage',
            'unsealed stop; compatible rollback waits full unknown window','SSE and owned cleanup'],
        same_window_condition='Require 429 after exact restore if the old minute has more than 3 seconds left; '
            'near expiry wait at most 3.05 seconds for natural rollover, then require 200. '
            'The branch, observation, old-window and dispatch timestamps are recorded before the request.',
        no_full_performance_matrix=True,default_replaced=False)
    p.save(OUT/'plan.json',plan)
    print(json.dumps(dict(plan=str(OUT/'plan.json'),package=receipt,limits=a.LIMITS,
                         validation_only=True,source_count=len(sources))))


if __name__ == '__main__':main()

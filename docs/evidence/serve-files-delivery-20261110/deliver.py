"""Publish accepted Files storage/transport with retained A08 rollback."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

sys.dont_write_bytecode = True
ROOT = Path.cwd()
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import bundle, publication
from tools.serve_service import package as p
from tools.serve_progress.prepare import copy_file

BASE = ROOT/'.q4t-work/serve-files-20261110'
PREVIOUS_REVISION, NEXT_REVISION = 32, 34
OUT = BASE/'release-01'
BUILD = ROOT/'.q4t-work/serve-custom-tools-20261108/engine-build'
DEFAULT = ROOT/'build'
STATE = DEFAULT/'service-native-state'
EVIDENCE = ROOT/'docs/evidence'


def state_hashes():
    return {str(f.relative_to(STATE)): p.sha(f) for f in STATE.rglob('*')
            if f.is_file() and (f.name == 'ledger.json' or
                               f.relative_to(STATE).parts[0].startswith('client-'))}


def main():
    p.require(not OUT.exists(), 'release exists; inspect before resuming')
    p.require(not subprocess.check_output(['git', 'status', '--porcelain']), 'commit sources first')
    accepted = p.read(EVIDENCE/'serve-files-business-20261110/decision.json')
    plan = p.read(BASE/'business-02/plan.json')
    p.require(accepted['status'] == 'PASS_FILES_TARGETED_BUSINESS' and
              accepted['all_nvml_ok'] and accepted['cleanup_confirmed'], 'business gate')
    reviewed = p.read(EVIDENCE/'serve-files-business-20261110/declaration-review.json')['changes']
    actual_changes = {n:dict(before=h, after=p.sha(ROOT/n)) for n,h in plan['sources'].items() if p.sha(ROOT/n) != h}
    p.require(actual_changes == reviewed, 'source drift outside reviewed declaration fix')
    p.require(all(p.sha(n) == h for n, h in plan['protected'].items()), 'default drift')
    p.require(p.sha(BUILD/'q4t') == accepted['binary_sha256'], 'engine drift')
    current = publication.status(STATE)
    p.require(current['ready'] and current['revision'] == PREVIOUS_REVISION, 'publication predecessor changed')
    old = (DEFAULT/'service').resolve(strict=True)
    old_hash = p.sha(old/'manifest.json')
    p.require(old_hash == current['current']['manifest_sha256'] ==
              'e8e754701d48fb1e4eee22f955b944eb07b62dac11ea25e6732469b32603bee0', 'A08 predecessor required')
    bundle.verify(old, old_hash)
    control_changes = {n for n in bundle.SOURCES if p.sha(ROOT/n) != p.sha(old/'source'/n)}
    allowed_controls = {'tools/serve_gateway/client_policy.py', 'tools/serve_gateway/gateway.py',
        'tools/serve_recovery/bundle.py', 'tools/serve_recovery/gateway_config.py',
        'tools/serve_recovery/quota_manager.py', 'tools/serve_recovery/quota_policy.py'}
    p.require(control_changes == allowed_controls, 'unreviewed control changes')
    native = p.read(BASE/'native-receipt.json')
    prior_native = p.read(old/'native-build.json')
    changed = {n for n,h in prior_native['source_files'].items() if p.sha(ROOT/n) != h}
    p.require(changed == {'include/q4t/server/http_observation.h',
        'include/q4t/service/ingress_transport.h', 'include/q4t/server/application_routes.h',
        'include/q4t/server/http_request.h', 'include/q4t/server/http_connection.h',
        'src/server/http_request.cpp', 'src/service/ingress_transport.cpp',
        'CMakeLists.txt', 'src/service/ingress_main.cpp'}, 'unexpected native source drift')
    p.require(set(native['source_files'])-set(prior_native['source_files']) ==
              {'include/q4t/server/file_limits.h'}, 'unexpected native source addition')
    build_diff = subprocess.check_output(['git','diff', 'd8e91bb2026ba7c43dca0ee84a434dff824a93da',
        'HEAD','--','CMakeLists.txt'],text=True)
    p.require(all(p.sha(ROOT/n) == h for n,h in native['source_files'].items()), 'native source drift')
    p.require(not (BASE/'build-files-native-release-01.stderr').read_bytes(), 'native build warning/error')
    for name,key in [('q4t-ingress','ingress_sha256'),('q4t-guardian','guardian_sha256'),
                     ('q4t-cuda-probe','cuda_probe_sha256')]:
        p.require(p.sha(BUILD/name) == native[key], 'native binary drift')
        p.require(native[key] == (accepted[key] if key == 'ingress_sha256' else prior_native[key]),
                  'unaccepted native binary')
    caps = p.read(BASE/'business-02/capabilities-1.json')
    config = p.read(BASE/'business-02/config-1.json')
    protocol_sources = p.read(BUILD/'generated/q4t/server/chat_protocol_sources.json')
    p.require(all(p.sha(ROOT/n) == h for n, h in protocol_sources.items()), 'embedded protocol drift')
    protocol = hashlib.sha256(''.join(n+':'+h+'\n' for n,h in sorted(protocol_sources.items())).encode()).hexdigest()
    p.require(protocol == accepted['protocol_sha256'] == caps['protocol_source_sha256'] ==
              config['protocol_source_sha256'], 'actual protocol drift')
    before = state_hashes()
    OUT.mkdir(mode=0o700)
    (OUT/'native-cmake-review.diff').write_text(build_diff)
    engine = OUT/'engine'
    for n in ('q4t', 'CMakeCache.txt'):
        copy_file(BUILD/n, engine/n)
    for n in ('q4t', 'q4t.release.json', 'service.release.json'):
        copy_file((DEFAULT/n).resolve(strict=True), OUT/'previous'/n)
    runtime = {n:h for n,h in plan['sources'].items() if n.startswith(('src/', 'include/', 'cmake/')) or n == 'CMakeLists.txt'}
    p.save(engine/'runtime-source.json', runtime)
    commit = subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
    p.save(OUT/'review.json', dict(source_commit=commit, native=native,
        controls={n:p.sha(ROOT/n) for n in bundle.SOURCES}, prior=current,
        business_state_before=before, runtime_sources=runtime,
        numeric_defaults_preserved=True, default_access_program_policy_configured=False,
        actual_business='Two cumulative starts, three actual generations and fourteen Files calls. Real native TLS and direct core SDK file IO, exact binary/document bytes, owner isolation, tool closure, original file recovery after own worker timeout, same-PID stateless SSE and precise usage. Parent native admission is an identity-bound test adapter; Guardian recovery is reused.',
        reuse='Multipart11, binary25, HTTP-stream32, Files-core16, transport34, package21 and driver/schema5 targeted CPU cases. C++/Python policy migration and incompatible old-package prevention are covered; Guardian/probe bytes unchanged. Declaration-only empty string cursor fix independently validates archived actual wire. No extra GPU or full quality/performance/long stability claim.',
        installation_checks=['sealed check','retained A08 selected check','sealed legacy check',
            'publish revision32','default check','default status','default observe','default publication'],
        model_starts=0, chats=0))
    decision = dict(status='PASS_TARGETED_FILES_RELEASE',
        acceptance='ACCEPT_TARGETED_FILES', actual_business=accepted, source_commit=commit,
        binary_sha256=accepted['binary_sha256'], protocol_sha256=protocol,
        source_review_sha256=p.sha(OUT/'review.json'), full_model_quality=False,
        performance_acceptance=False, long_stability_accepted=False, remaining_master_tasks_open=True)
    p.save(OUT/'decision.json', decision)
    metadata = p.read(OUT/'previous/q4t.release.json')
    metadata.update(version='serve-files-20261110', source_commit=commit,
        binary_sha256=accepted['binary_sha256'], acceptance='ACCEPT_TARGETED_FILES',
        acceptance_decision=str(OUT/'decision.json'), acceptance_decision_sha256=p.sha(OUT/'decision.json'),
        build_cache=str(engine/'CMakeCache.txt'), build_cache_sha256=p.sha(engine/'CMakeCache.txt'),
        tested_source_manifest=str(engine/'runtime-source.json'), tested_source_manifest_sha256=p.sha(engine/'runtime-source.json'),
        observed_configuration=config, inherited_release=dict(path=str(OUT/'previous/q4t.release.json'),sha256=p.sha(OUT/'previous/q4t.release.json')),
        previous_binary=str(OUT/'previous/q4t'), previous_release=str(OUT/'previous/q4t.release.json'),
        source_note='Native Files multipart storage and bounded binary streaming, explicit client v4/public v5 capacity with legacy limits preserved, safe v4-to-v3 migration conserving usage. Targeted CPU and bounded actual SDK/tool/IO-isolation business accepted. Exact published empty page string cursor schema corrected against official Files HTTP and archived wire without production changes. Default seed limits remain unchanged; test 32MiB file capacity and two clients are separately observed. Uploads, native file input consumption, Batch and all master work remain required.')


    declarations = engine/'declarations'
    declarations.mkdir(mode=0o700)
    p.save(declarations/'capabilities.json', caps)
    p.save(declarations/'configuration.json', config)
    metadata['chat_protocol'].update(caps, protocol_sha256=protocol,
        declaration_evidence='Actual Files discovery used isolated v4 two-client state, file capacity32MiB and output384. Default legacy policy/quota/output256 are preserved without expansion; existing SDK/history/key contracts retained. Schema-only empty string cursor correction verified from archived real wire. No generated builtin file consumers claimed.')
    for name in p.declaration_schemas(p.APPLICATION_DECLARATION_PROFILE):
        path = declarations/'schemas'/(name+'.json')
        source = {'config': ROOT/'docs/serve-config-v1.schema.json',
                  'application': ROOT/'docs/serve-application-v1.schema.json',
                  'openapi': ROOT/'docs/serve-openapi-bundle-v1.json'}.get(
                      name, old/'engine/declarations/schemas'/(name+'.json'))
        copy_file(source, path)
        metadata['chat_protocol'][name+'_schema'] = str(path)
        metadata['chat_protocol'][name+'_schema_sha256'] = p.sha(path)
    metadata['release_declarations'] = dict(profile=p.APPLICATION_DECLARATION_PROFILE,
        directory=str(declarations),binary_sha256=accepted['binary_sha256'],protocol_sha256=protocol,
        files={n:p.sha(declarations/n) for n in p.declaration_files(p.APPLICATION_DECLARATION_PROFILE)})
    metadata['files_acceptance'] = dict(
        native_multipart=True, binary_streaming=True, authenticated_scope=True,
        atomic_publication=True, separate_capacity_and_storage_quotas=True,
        safe_legacy_policy_migration=True, same_model_io_failure_sse=True,
        original_file_recovery=True, actual_sdk_document_tool=True,
        starts=2, generations=3, file_operations=14,
        uploads_pending=True, file_input_resolution_pending=True, batch_pending=True)
    notices = metadata['third_party_notices']
    notices['source_directory'] = str(engine/'licenses/structured_output')
    notices['files'] = {}
    for file in sorted((ROOT/'third_party/licenses').rglob('*')):
        if file.is_file():
            name = str(file.relative_to(ROOT/'third_party/licenses'))
            copy_file(file, engine/'licenses'/name)
            if name.startswith('structured_output/'):
                notices['files'][name[len('structured_output/'):]] = dict(sha256=p.sha(file),text=file.read_text())
    metadata['application_notices'] = dict(source_directory=str(engine/'licenses/openai-node'),files={
        str(f.relative_to(engine/'licenses/openai-node')):p.sha(f) for f in (engine/'licenses/openai-node').rglob('*') if f.is_file()})
    copy_file('/usr/share/doc/libidn2-0/copyright',engine/'licenses/system-libidn2-copyright')
    copy_file('/usr/share/doc/libssl3t64/copyright',engine/'licenses/system-openssl-copyright')
    clients = metadata['client_artifacts']
    for name, digest in clients['files'].items():
        p.require(p.sha(ROOT/name) == digest, 'prior accepted client changed')
        copy_file(ROOT/name, OUT/'clients'/name)
    metadata['client_artifacts'] = dict(directory=str(OUT/'clients'),files=clients['files'],server_dependency=False)
    p.save(engine/'q4t.release.json', metadata)
    p.verify_declarations(metadata, required=True)
    p.save(OUT/'native-build.json', native)
    receipt = bundle.prepare(engine/'q4t',engine/'q4t.release.json',p.read(old/'engine/manifest.json')['model_dir'],
        BUILD/'q4t-guardian',BUILD/'q4t-cuda-probe',None,old/'gateway-policy.json',old/'engine/client-policy.json',
        OUT/'bundle',native_receipt=OUT/'native-build.json',native_ingress=BUILD/'q4t-ingress',capacity_config=dict(version=1))
    p.save(OUT/'receipt.json', receipt)
    package = Path(receipt['package']);digest = receipt['manifest_sha256'];cli=package/'engine/q4t'
    p.require(bundle.verify(package,digest)['version'] == 11, 'package version')
    p.require(p.read(package/'engine/manifest.json')['settings'] == p.read(old/'engine/manifest.json')['settings'], 'default capacity drift')
    for name in ('gateway-policy.json','engine/client-policy.json'):
        p.require(p.sha(package/name) == p.sha(old/name), 'client seed drift')
    p.save(OUT/'rollback.json',dict(previous=current['current'],previous_revision=PREVIOUS_REVISION,
        command='Use current q4t service publish-rollback with retained generation/current revision/new request UUID; never restore business journals.'))
    rows = []

    def run(label, args):
        argv = [str(x) for x in args];start=time.monotonic()
        value = subprocess.run(argv,stdin=subprocess.DEVNULL,capture_output=True,timeout=45)
        (OUT/(label+'.stdout')).write_bytes(value.stdout);(OUT/(label+'.stderr')).write_bytes(value.stderr)
        row = dict(label=label,argv=argv,returncode=value.returncode,seconds=time.monotonic()-start)
        rows.append(row);p.save(OUT/(label+'.json'),row)
        p.require(value.returncode == 0, 'command failed: '+label)
        p.require(state_hashes() == before, 'business state changed: '+label)
        return json.loads(value.stdout)

    state_args = ['--state', STATE]
    try:
        p.require(run('sealed-check',[cli,'service','check'])['status']=='PASS','sealed package')
        p.require(run('previous-check',[cli,'service','check',*state_args,'--package',old,
            '--manifest-sha256',old_hash])['desired']=='STOPPED','previous selected')
        p.require(run('legacy-check',[package/'bin/q4t','service','check'])['status']=='PASS','legacy sealed')
        service = p.read(OUT/'previous/service.release.json')
        service.update(acceptance=decision['acceptance'],acceptance_record=str(OUT/'decision.json'),
            acceptance_sha256=p.sha(OUT/'decision.json'),previous_acceptance_record=service['acceptance_record'],
            package=str(package),manifest_sha256=digest,engine_binary_sha256=receipt['engine_binary_sha256'],
            engine_release_sha256=p.sha(package/'engine/q4t.release.json'),engine_metadata_unchanged=False,
            control_review=str(OUT/'review.json'),client_artifacts=str(OUT/'clients'),files_targeted_accepted=True,
            rollback_receipt=str(OUT/'rollback.json'),rollback_sha256=p.sha(OUT/'rollback.json'),
            scope=service['scope']+' Native Files multipart/streaming, explicit file capacities with old limits preserved, actual SDK document tool closure and same-PID storage failure isolation. v11 policy/version guards and explicit safe quota rollback. Default credentials/capacity/usage remain unchanged; Uploads/file-input/Batch and remaining master work continue.')
        p.save(OUT/'service.release.json',service)
        request = str(uuid.uuid4())
        p.save(OUT/'publish-request.json',dict(request_id=request,expected_revision=PREVIOUS_REVISION,
            expected_manifest_sha256=old_hash,target_manifest_sha256=digest))
        published = run('publish',[cli,'service','publish',*state_args,'--default-root',DEFAULT,
            '--package',package,'--manifest-sha256',digest,'--service-release',OUT/'service.release.json',
            '--expected-manifest-sha256',old_hash,'--expected-revision',PREVIOUS_REVISION,'--request-id',request])
        p.require(published['durable'] and published['revision']==NEXT_REVISION,'publish result')
        for name in ('check','status','observe'):
            value = run('default-'+name,[DEFAULT/'q4t','service',name,*state_args])
            p.require(value['status']=='PASS' if name=='check' else value['state']=='STOPPED','default '+name)
        final = run('default-publication',[DEFAULT/'q4t','service','publish-status',*state_args])
        p.require(final['ready'] and final['revision']==NEXT_REVISION and final['current']['manifest_sha256']==digest,'publication selection')
        p.save(OUT/'result.json',dict(status='PASS_FILES_DELIVERED',current=final['current'],
            previous=current['current'],publication_revision=NEXT_REVISION,commands=rows,state='STOPPED',
            business_state_preserved=state_hashes()==before,model_starts=0,chats=0,gpu_queries=0,
            default_changed=True,main_merged=False,remaining_master_tasks_open=True))
    except BaseException as error:
        p.save(OUT/'result.json',dict(status='FAILED',commands=rows,category=type(error).__name__,detail=str(error),
            business_state_preserved=state_hashes()==before,recovery='Inspect publication and use same request resume only if already prepared.'))
        raise
    print(json.dumps(dict(status='PASS_FILES_DELIVERED',package=digest,revision=NEXT_REVISION,commands=len(rows))))


if __name__ == '__main__':
    os.umask(0o077)
    main()

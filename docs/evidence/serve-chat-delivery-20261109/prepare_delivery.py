"""Seal accepted application APIs without repeating unchanged business tests."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path.cwd().resolve()
sys.path.insert(0, str(ROOT))
from tools.serve_service import package as p
from tools.serve_recovery import bundle

BASE = ROOT / '.q4t-work/serve-chat-storage-20261109'
BUILD = ROOT / '.q4t-work/serve-custom-tools-20261108/engine-build'
OUT = BASE / 'release-01'
EVIDENCE = ROOT / 'docs/evidence'
read, save, sha, require = p.read, p.save, p.sha, p.require


def copy_file(source, destination):
    source, destination = Path(source), Path(destination)
    p.regular(source)
    require(not destination.exists(), 'refuse overwrite: ' + str(destination))
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    destination.chmod(0o500 if os.access(source, os.X_OK) else 0o400)
    require(sha(source) == sha(destination), 'copy identity')


def main():
    require(not OUT.exists(), 'release already exists')
    require(not subprocess.check_output(['git', 'status', '--porcelain']),
            'commit sources before packaging')
    plan = read(BASE / 'business-03/plan.json')
    accepted = read(EVIDENCE / 'serve-chat-business-20261109/decision.json')
    require(accepted['status'] == 'ACCEPT_TARGETED_STORED_CHAT' and
            accepted['lease_status'] == 'COMPLETED' and accepted['all_nvml_ok'],
            'actual stored Chat acceptance')
    require(sha(BUILD / 'q4t') == accepted['binary_sha256'] == plan['binary_sha256'],
            'accepted runtime changed')
    require(all(sha(ROOT / n) == h for n, h in plan['sources'].items()),
            'sources changed after actual business')
    require(all(sha(n) == h for n, h in plan['protected'].items()),
            'default changed before delivery')
    groups = ('serve-chat-storage-20261109', 'serve-chat-storage-prefix-20261109',
              'serve-chat-unknown-20261109', 'serve-chat-business-20261109',
              'serve-application-delivery-20261109')
    indices = {}
    for group in groups:
        directory = EVIDENCE / group
        index = read(directory / 'index.json')
        files = ({row['path']: row['sha256'] for row in index['files']}
                 if isinstance(index.get('files'), list) else index)
        files = {n: h['sha256'] if isinstance(h, dict) else h for n, h in files.items()}
        require(all(sha(directory / n) == h for n, h in files.items()),
                'evidence drift: ' + group)
        indices[group] = dict(index_sha256=sha(directory / 'index.json'), files=len(files))
    passed = [
        'serve-chat-storage-20261109/core-cpu-01',
        'serve-chat-storage-20261109/integration-cpu-01',
        'serve-chat-storage-20261109/query-cpu-01',
        'serve-chat-storage-20261109/sdk-03',
        'serve-chat-storage-20261109/declarations-01',
        'serve-chat-storage-20261109/native-01',
        'serve-chat-storage-20261109/tls-01',
        'serve-chat-storage-20261109/nginx-01',
        'serve-chat-storage-20261109/build-04',
        'serve-chat-unknown-20261109/unknown-cpu-01',
        'serve-chat-unknown-20261109/unknown-inventory-01',
        'serve-chat-unknown-20261109/build-07',
    ]
    require(all(read(EVIDENCE / n / 'result.json')['exit_code'] == 0 for n in passed),
            'targeted prerequisite failed')
    # Three SDK cases passed in sdk-01; the real pagination/parser issue was
    # repaired separately in sdk-03. Keep the original suite failure intact.
    partial = EVIDENCE / 'serve-chat-storage-20261109/sdk-01'
    partial_log = (partial / 'stdout.log').read_text()
    require(read(partial / 'result.json')['exit_code'] != 0 and
            all(('✔ '+case+' (') in partial_log for case in (
                'official stored Chat create/update/retrieve/delete survives process restart',
                'official SDK stream leaves a complete retrievable multi-choice record',
                'official authentication scopes and errors do not expose or mutate other clients')), 'unchanged SDK evidence')
    caps = read(BASE / 'business-03/capabilities-1.json')
    config = read(BASE / 'business-03/config-1.json')
    protocol = caps['protocol_source_sha256']
    protocol_sources = read(BUILD / 'generated/q4t/server/chat_protocol_sources.json')
    digest = hashlib.sha256(''.join(n + ':' + h + '\n' for n, h in
                                   sorted(protocol_sources.items())).encode()).hexdigest()
    require(digest == protocol == config['protocol_source_sha256'] and
            all(sha(ROOT / n) == h for n, h in protocol_sources.items()),
            'embedded protocol source identity')
    old = (ROOT / 'build/service').resolve(strict=True)
    bundle.verify(old, sha(old / 'manifest.json'))
    control = {n: sha(ROOT / n) for n in bundle.SOURCES}
    changed = {n: dict(before=sha(old / 'source' / n), after=h)
               for n, h in control.items() if sha(old / 'source' / n) != h}
    require(set(changed) == {'tools/serve_gateway/gateway.py'},
            'unexpected control change')
    require(sha(BUILD / 'q4t-ingress') ==
            'cc35816ef1a92776e532e1da9ba7752b236d5e179f71912fca47eeaa4d543818',
            'native ingress changed after targeted routes/TLS tests')
    require(sha(BUILD / 'q4t-guardian') == sha(old / 'bin/q4t-guardian'),
            'Guardian changed')
    OUT.mkdir(mode=0o700)
    engine = OUT / 'engine'; engine.mkdir()
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    for name in plan['sources']: copy_file(ROOT / name, OUT / 'source' / name)
    runtime = {n: sha(ROOT / n) for n in subprocess.check_output(
        ['git', 'ls-files', 'src', 'include', 'cmake', 'CMakeLists.txt'], text=True).splitlines()}
    save(engine / 'runtime-source.json', runtime)
    save(OUT / 'source-snapshot.json', dict(commit=commit,
        original_test_commit=plan['source_commit'], current=plan['sources'],
        runtime_unchanged_after_final_business=True))
    for name in ('q4t', 'CMakeCache.txt'): copy_file(BUILD / name, engine / name)
    for name in ('q4t', 'q4t.release.json', 'service.release.json'):
        copy_file(ROOT / 'build' / name, OUT / 'previous' / name)
    declarations = engine / 'declarations'; declarations.mkdir()
    save(declarations / 'capabilities.json', caps)
    save(declarations / 'configuration.json', config)
    schemas = dict(config='serve-config-v1.schema.json',
        message='serve-message-profile-v3.schema.json', preflight='serve-preflight-v1.schema.json',
        reasoning='serve-reasoning-v1.schema.json', response='serve-transport-v1.schema.json',
        stop='serve-stop-v1.schema.json', thinking='serve-thinking-v1.schema.json',
        completion_request='serve-completion-request-v1.schema.json',
        completion_response='serve-completion-transport-v1.schema.json',
        application='serve-application-v1.schema.json', openapi='serve-openapi-bundle-v1.json')
    for name, file in schemas.items():
        copy_file(ROOT / 'docs' / file, declarations / 'schemas' / (name + '.json'))
    decision = dict(status='PASS_TARGETED_STORED_CHAT_DELIVERY',
        acceptance='ACCEPT_TARGETED_STORED_CHAT', binary_sha256=accepted['binary_sha256'],
        protocol_sha256=protocol, source_commit=commit, original_test_commit=plan['source_commit'],
        evidence=indices, accepted_business=accepted, passed_cpu=passed,
        previous_application_acceptance_reused=str(EVIDENCE / 'serve-application-delivery-20261109/README.md'),
        runtime_unchanged_after_final_business=True, additional_gpu_tests=0,
        performance_acceptance=False, full_model_quality_run=False,
        long_stability_accepted=False, remaining_master_tasks_open=True)
    save(OUT / 'decision.json', decision)
    save(OUT / 'control-review.json', dict(previous_package=str(old), files=control,
        changed=changed, unchanged_count=len(control) - len(changed),
        coverage='Native stored Chat routes/TLS and optional nginx full application routes '
        'have targeted actual ingress tests. Guardian/restart policy unchanged. '
        'Portable v3 application declarations reuse accepted packaging evidence.',
        reused_cpu={n: sha(EVIDENCE / n / 'result.json') for n in passed},
        partially_failed_sdk=str(partial)))
    metadata = read(ROOT / 'build/q4t.release.json')
    metadata.update(version='serve-stored-chat-20261109', binary_sha256=accepted['binary_sha256'],
        source_commit=commit, source_note='Native stored Chat CRUD/history, actual multi-choice JSON/SSE/logprobs and tools; '
        'honest unknown durable commit errors and same-model recovery; application APIs retained.',
        build_cache=str(engine / 'CMakeCache.txt'), build_cache_sha256=sha(engine / 'CMakeCache.txt'),
        tested_source_manifest=str(engine / 'runtime-source.json'),
        tested_source_manifest_sha256=sha(engine / 'runtime-source.json'),
        acceptance=decision['acceptance'], acceptance_decision=str(OUT / 'decision.json'),
        acceptance_decision_sha256=sha(OUT / 'decision.json'),
        inherited_release=dict(path=str(OUT / 'previous/q4t.release.json'),
            sha256=sha(OUT / 'previous/q4t.release.json')),
        previous_binary=str(OUT / 'previous/q4t'), previous_release=str(OUT / 'previous/q4t.release.json'),
        observed_configuration=config)
    metadata['chat_protocol'].update(caps)
    metadata['chat_protocol'].update(protocol_sha256=protocol,
        declaration_evidence='Exact runtime discovery and stored Chat JSON/SSE/tools/fault evidence; '
        'unaffected prior Responses/Conversations and other protocols reuse accepted evidence.')
    for name in schemas:
        file = declarations / 'schemas' / (name + '.json')
        metadata['chat_protocol'][name + '_schema'] = str(file)
        metadata['chat_protocol'][name + '_schema_sha256'] = sha(file)
    metadata['release_declarations'] = dict(profile=p.APPLICATION_DECLARATION_PROFILE,
        directory=str(declarations), binary_sha256=accepted['binary_sha256'], protocol_sha256=protocol,
        files={n: sha(declarations / n) for n in p.declaration_files(p.APPLICATION_DECLARATION_PROFILE)})
    metadata['scope'] = dict(production='Text/MTP, opt-in vision/schema tools, native Chat/Completions/Models '
        'and stored Chat/Responses/Conversations/background with isolated durable application state.',
        correctness='Targeted CPU/SDK and actual application/fault business; no full model quality rerun.',
        performance='Functional delivery; no performance acceptance claim.',
        installation_health='Offline package verification and stopped-service help/status, no model start.',
        boundaries='Remaining master-plan capabilities still required; user owns long stability.')
    metadata['application_acceptance'] = dict(decision=str(OUT / 'decision.json'),
        responses=True, conversations=True, background=True, cancellation=True,
        foreground_unknown_commit_reconciliation=True, stored_chat=True,
        public_unknown_commit_semantics=True, full_openai_compatibility=False)
    notices = metadata['third_party_notices']
    notices['source_directory'] = str(engine / 'licenses/structured_output'); notices['files'] = {}
    for file in sorted((ROOT / 'third_party/licenses').rglob('*')):
        if file.is_file():
            name = str(file.relative_to(ROOT / 'third_party/licenses'))
            copy_file(file, engine / 'licenses' / name)
            if name.startswith('structured_output/'):
                notices['files'][name[len('structured_output/'):]] = dict(sha256=sha(file), text=file.read_text())
    metadata['application_notices'] = dict(source_directory=str(engine / 'licenses/openai-node'),
        files={str(f.relative_to(engine / 'licenses/openai-node')): sha(f)
               for f in (engine / 'licenses/openai-node').rglob('*') if f.is_file()})
    copy_file('/usr/share/doc/libidn2-0/copyright', engine / 'licenses/system-libidn2-copyright')
    save(engine / 'q4t.release.json', metadata)
    p.verify_declarations(metadata, required=True)
    native = dict(version=1, source_files={n: sha(ROOT / n) for n in sorted(bundle.NATIVE_SOURCES)},
        guardian_sha256=sha(BUILD / 'q4t-guardian'), ingress_sha256=sha(BUILD / 'q4t-ingress'),
        cuda_probe_sha256=sha(old / 'bin/q4t-cuda-probe'), provenance=dict(
            build=str(EVIDENCE / 'serve-chat-storage-20261109/build-04/result.json'),
            native_cpu=[str(EVIDENCE / n / 'result.json') for n in passed if
                        n.endswith('/native-01') or n.endswith('/tls-01')],
            cuda_probe_reused_from=str(old / 'native-build.json')))
    require(native['source_files']['src/service/cuda_probe.cu'] ==
        read(old / 'native-build.json')['source_files']['src/service/cuda_probe.cu'], 'CUDA probe drift')
    save(OUT / 'native-build.json', native)
    receipt = bundle.prepare(engine / 'q4t', engine / 'q4t.release.json', plan['model'],
        BUILD / 'q4t-guardian', old / 'bin/q4t-cuda-probe', None,
        old / 'gateway-policy.json', old / 'engine/client-policy.json', OUT / 'bundle',
        native_receipt=OUT / 'native-build.json', native_ingress=BUILD / 'q4t-ingress')
    save(OUT / 'receipt.json', receipt)
    bundle.verify(receipt['package'], receipt['manifest_sha256'])
    require(sha(OUT / 'bundle/gateway-policy.json') == sha(old / 'gateway-policy.json') and
            sha(OUT / 'bundle/engine/client-policy.json') == sha(old / 'engine/client-policy.json'),
            'policy/key/quota changed')
    print(json.dumps(dict(release=receipt, changed_control=len(changed),
        protocol_sources=len(protocol_sources), tested_sources=len(plan['sources']),
        additional_gpu_tests=0)))


if __name__ == '__main__': main()

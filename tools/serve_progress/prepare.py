"""Freeze a pending R03 validation candidate; never an accepted release."""
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

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_progress import acceptance as a
from tools.serve_service import package as p
from tools.serve_recovery import bundle
from tools.examples import chat_client as client
sys.path.insert(0, str(ROOT / 'tools/examples'))
from project_records import TOOLS

BASE = ROOT / '.q4t-work/serve-progress-20261109'
BUILD = ROOT / '.q4t-work/serve-custom-tools-20261108/engine-build'
OUT = BASE / 'business-01'


def copy_file(source, destination):
    source, destination = Path(source), Path(destination)
    p.regular(source)
    p.require(not destination.exists(), 'refuse to overwrite validation evidence')
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    destination.chmod(0o500 if os.access(source, os.X_OK) else 0o400)
    p.require(p.sha(source) == p.sha(destination), 'copy identity mismatch')


def requests():
    result = {}
    for label in ('initial', 'observation-lost'):
        for stream in (False, True):
            result[label+('-sse' if stream else '-json')] = client.make_request([
                dict(role='user', content='Compute 17 + 25. Reply with the number only.')],
                [], stream=stream, max_tokens=128, timeout_ms=60000)
    tools = copy.deepcopy(TOOLS[:1])
    tools[0]['function']['strict'] = True
    tools[0]['function']['parameters']['properties'] = dict(
        path=dict(type='string', enum=['docs/MODEL.md']),
        start_line=dict(type='integer', enum=[1]),
        line_count=dict(type='integer', enum=[3]))
    tool = client.make_request([dict(role='user', content=
        'Read the first 3 lines of docs/MODEL.md with read_project_record. '
        'Then reply only with the exact citation returned by the tool.')],
        tools, stream=True, max_tokens=256, timeout_ms=60000)
    tool.update(tool_choice=dict(type='function', function=dict(name='read_project_record')),
        parallel_tool_calls=False)
    result['tool'] = tool
    follow = client.make_request(tool['messages'], tools, stream=False,
        max_tokens=256, timeout_ms=60000)
    follow['tool_choice'] = 'none'
    result['follow'] = follow
    return result


def main():
    os.umask(0o077)
    p.require(not OUT.exists(), 'R03 validation output already exists')
    p.require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
        'commit reviewed driver before freezing sources')
    cpu_log = (BASE / 'driver-cpu-01/stderr.log').read_text()
    p.require(p.read(BASE / 'driver-cpu-01/result.json')['exit_code'] == 1 and
        all(name+' (' in cpu_log and any(line.startswith(name+' (') and
            line.endswith(' ... ok') for line in cpu_log.splitlines()) for name in (
            'test_exception_always_detaches_owned_main',
            'test_only_owned_main_thread_pauses_while_other_thread_runs',
            'test_stale_birth_and_non_descendant_are_rejected_before_ptrace',
            'test_work_is_reserved_before_dispatch_and_never_refunded')) and
        p.read(BASE / 'driver-cpu-02/result.json')['exit_code'] == 0,
        'targeted validation driver CPU prerequisites')
    old = (ROOT / 'build/service').resolve(strict=True)
    bundle.verify(old, p.sha(old / 'manifest.json'))
    p.require(p.sha(BUILD / 'q4t') ==
        '1f3d0de49935ac6b586b67ce1e74673f781480333aba48652836f50eadd0e2be',
        'CPU-verified candidate binary changed')
    for port in (18761, 18762, 18763):
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('127.0.0.1', port))
    protocol_sources = p.read(BUILD / 'generated/q4t/server/chat_protocol_sources.json')
    p.require(all(p.sha(ROOT / n) == h for n, h in protocol_sources.items()),
        'candidate embedded source drift')
    protocol = hashlib.sha256(''.join(n+':'+h+'\n' for n, h in
        sorted(protocol_sources.items())).encode()).hexdigest()
    OUT.mkdir(mode=0o700)
    native = p.read(old / 'native-build.json')
    changed = {n: dict(old=h, new=p.sha(ROOT / n))
        for n, h in native['source_files'].items() if p.sha(ROOT / n) != h}
    p.require(set(changed) <= {'CMakeLists.txt', 'tests/host/CMakeLists.txt'},
        'unexpected native ingress/Guardian implementation change')
    native.update(source_files={n: p.sha(ROOT / n) for n in bundle.NATIVE_SOURCES},
        provenance=dict(reused_from=str(old / 'native-build.json'),
            unchanged_executables=True, changed_build_registration=changed,
            engine_build=str(BASE / 'build-06/result.json')))
    p.save(OUT / 'native-build.json', native)
    policy = p.read(old / 'gateway-policy.json')
    policy.update(listen_port=18761, upstream_port=18762,
        upstream_operations_port=18763)
    for row in policy['clients']:
        row['api_key'] = 'r03-validation-'+secrets.token_hex(24)
    backend = p.read(old / 'engine/client-policy.json')
    backend['gateway_token'] = secrets.token_hex(32)
    p.save(OUT / 'gateway-policy.json', policy)
    p.save(OUT / 'client-policy.json', backend)
    for name in ('q4t', 'CMakeCache.txt'):
        copy_file(BUILD / name, OUT / 'candidate' / name)
    declarations = OUT / 'candidate/declarations'
    declarations.mkdir()
    for name in p.declaration_files(p.APPLICATION_DECLARATION_PROFILE):
        source = old / 'engine/declarations' / name
        if name.endswith('.json') and not name.startswith('schemas/'):
            value = p.read(source)
            value['protocol_source_sha256'] = protocol
            p.save(declarations / name, value)
        else:
            copy_file(source, declarations / name)
    caps = p.read(declarations / 'capabilities.json')
    metadata = p.read(ROOT / 'build/q4t.release.json')
    decision = dict(status='PENDING_REAL_BUSINESS_R03',
        validation_only=True, limits=a.LIMITS,
        binary_sha256=p.sha(OUT / 'candidate/q4t'), protocol_sha256=protocol,
        declaration_basis='Expected unchanged public contract copied from accepted '
        'release with the compiled source identity; not yet observed on candidate.',
        actual_observations_required=True)
    p.save(OUT / 'pending-decision.json', decision)
    metadata.update(validation_only=True, acceptance='VALIDATION_ONLY_R03',
        version='r03-validation-only', binary_sha256=decision['binary_sha256'],
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
        build_cache=str(OUT / 'candidate/CMakeCache.txt'),
        build_cache_sha256=p.sha(OUT / 'candidate/CMakeCache.txt'),
        acceptance_decision=str(OUT / 'pending-decision.json'),
        acceptance_decision_sha256=p.sha(OUT / 'pending-decision.json'),
        observed_configuration=p.read(declarations / 'configuration.json'),
        validation_declaration_basis=decision['declaration_basis'])
    metadata['chat_protocol'].update(caps, protocol_sha256=protocol)
    for name in p.declaration_schemas(p.APPLICATION_DECLARATION_PROFILE):
        file = declarations / 'schemas' / (name+'.json')
        metadata['chat_protocol'][name+'_schema'] = str(file)
        metadata['chat_protocol'][name+'_schema_sha256'] = p.sha(file)
    metadata['release_declarations'] = dict(profile=p.APPLICATION_DECLARATION_PROFILE,
        directory=str(declarations), binary_sha256=metadata['binary_sha256'],
        protocol_sha256=protocol, files={n: p.sha(declarations / n)
            for n in p.declaration_files(p.APPLICATION_DECLARATION_PROFILE)})
    p.save(OUT / 'candidate/q4t.release.json', metadata)
    expected = dict(metadata=a.fingerprint(metadata), decision=a.fingerprint(decision))
    try:
        p.accepted_release(metadata, decision)
    except RuntimeError:
        pass
    else:
        raise RuntimeError('production verifier accepted a pending candidate')
    p.accepted_release = a.validation_gate(p.accepted_release, expected)
    model = p.read(old / 'engine/manifest.json')['model_dir']
    receipt = bundle.prepare(OUT / 'candidate/q4t', OUT / 'candidate/q4t.release.json',
        model, old / 'bin/q4t-guardian', old / 'bin/q4t-cuda-probe', None,
        OUT / 'gateway-policy.json', OUT / 'client-policy.json', OUT / 'bundle',
        native_receipt=OUT / 'native-build.json', native_ingress=old / 'bin/q4t-ingress')
    p.save(OUT / 'receipt.json', receipt)
    sources = set(protocol_sources) | set(bundle.SOURCES) | set(bundle.NATIVE_SOURCES)
    sources |= set(subprocess.check_output(['git', 'ls-files', 'tools/serve_progress'],
        text=True).splitlines()) | {'tools/serve_recovery/acceptance.py',
        'tools/examples/chat_client.py', 'tools/examples/project_records.py', 'docs/MODEL.md'}
    protected = [ROOT / 'build' / n for n in
        ('q4t', 'q4t.release.json', 'service.release.json')]
    protected += [old / n for n in ('manifest.json', 'gateway-policy.json',
        'engine/client-policy.json')]
    checks = {'protocol_source_sha256': protocol, 'model': caps['model'],
        'listeners.public.port': 18762, 'listeners.operations.port': 18763,
        'client_policy.enabled': True, 'client_policy.clients': 2,
        'client_governance.fair_queue': True, 'client_governance.policy_version': 2,
        'max_context_tokens.effective': 8192, 'max_sequences.effective': 1,
        'prefill_chunk_tokens.effective': 2048, 'default_output_tokens.effective': 256,
        'runtime.mtp_enabled': True, 'runtime.mtp_k': 3,
        'runtime.structured_backend': True, 'runtime.media_enabled': False,
        'application_state.core_readiness_dependency': False}
    plan = dict(version=1, scenario='native_progress', output=str(OUT),
        package=receipt, source_commit=metadata['source_commit'],
        sources={n: p.sha(ROOT / n) for n in sorted(sources)},
        protected=[dict(path=str(n), sha256=p.sha(n)) for n in protected],
        validation_release=expected, configuration_checks=checks,
        limits=a.LIMITS, requests=requests())
    p.save(OUT / 'plan.json', plan)
    print(json.dumps(dict(plan=str(OUT / 'plan.json'), package=receipt,
        limits=a.LIMITS, validation_only=True, sources=len(sources))))


if __name__ == '__main__':
    main()

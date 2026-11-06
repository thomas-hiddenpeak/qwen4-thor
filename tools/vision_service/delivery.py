"""Freeze/install the scoped visual release, without model or GPU execution.

Uses the existing verified-copy and per-file atomic replacement primitives.
Install and rollback serialize with the shared GPU lock, reject unknown pairs,
and keep a durable recovery marker. Two files are not power-loss atomic.
"""
import argparse
import contextlib
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.verify.startup_delivery import deploy as h
from tools.serve_service import package as declarations

BASE = ROOT / '.q4t-work/vision-production-20261106'
PACKAGE = BASE / 'release-v1'
OUTPUT = BASE / 'delivery-v1'
MARKER = ROOT / 'build/vision-service-recovery.json'
NAMES = ('q4t', 'q4t.release.json')
OLD = dict(zip(NAMES, (
    'aa528a6839ed629fcea2aed036f4c8f0bdc14617ac00fdc31156e6e58c3e18d0',
    '46aa73bd678ace6c79a8c4946c541e2e53ac7808c215374626c59c46ee90a11b')))
NEW = 'ea1b474d7d69808696e697346a60ee09bb9ecaca9bfb99925b78dcdbda7a4550'
AUDIT = '8d4c8e9e99205955c87ddc6008a5823b676eb35695f8cef3b4c28b79bbd19681'


def copy_file(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    return h.copy_checked(h.identity(source), target)


def hashes(directory):
    return {name: h.sha(directory/name) for name in NAMES}


def checked_evidence():
    audit = BASE/'audit-03'
    binding = h.read(audit/'binding.json')
    h.require(h.sha(audit/'result.json') == binding['report_sha256'] == AUDIT,
              'independent acceptance changed')
    h.require(h.sha(audit/'originals.json') == binding['originals_sha256'],
              'original evidence manifest changed')
    h.require(h.sha(ROOT/'tools/vision_service/report.py') ==
              binding['verifier_sha256'], 'independent verifier changed')
    for name, row in h.read(audit/'originals.json').items():
        path = (BASE/name).resolve(strict=True)
        h.require(path.is_relative_to(BASE) and h.sha(path) == row['sha256']
                  and path.stat().st_size == row['bytes'], 'evidence: '+name)
    result = h.read(audit/'result.json')
    h.require(result['candidate_sha256'] == NEW and
              result['default_text_mtp_protection'] and
              result['image_business_accepted'] and
              not result['structured_temporal_direction_accepted'] and
              len(result['temporal_quality_failures']) == 4,
              'scoped decision differs')
    return result


def prepare():
    result = checked_evidence()
    h.require(hashes(ROOT/'build') == OLD, 'default drift')
    old = h.verify_pair(ROOT/'build/q4t', ROOT/'build/q4t.release.json', OLD['q4t'])
    h.require(not list((ROOT/'build').glob('*recovery*.json')), 'pending recovery')
    plan = h.read(BASE/'phase-07/plan.json')
    h.require(h.sha(BASE/'engine/q4t') == NEW, 'candidate changed')
    PACKAGE.mkdir(mode=0o700)
    prior, candidate = PACKAGE/'previous', PACKAGE/'candidate'
    for name in NAMES:
        copy_file(ROOT/'build'/name, prior/name)
    # Keep the old sidecar byte-exact. Its original dependency paths stay valid;
    # copy core files too, and make the retained external dependencies explicit.
    dependencies = {}
    for key in ('build_cache', 'acceptance_decision', 'tested_source_manifest'):
        source = Path(old[key])
        dependencies[key] = h.identity(source)
        copy_file(source, prior/'dependencies'/key)
    for name in declarations.DECLARATION_FILES:
        source = Path(old['release_declarations']['directory'])/name
        dependencies['declarations/'+name] = h.identity(source)
        copy_file(source, prior/'declarations'/name)
    for key in ('operations_rules', 'rollback_operations_rules'):
        h.check_identity(old[key])
        dependencies[key] = old[key]
        copy_file(old[key]['path'], prior/'dependencies'/key)
    h.save(prior/'dependency-inventory.json', dict(files=dependencies,
        retained_subset_artifact=old['subset_artifact'],
        retained_subset_manifest_sha256=old['subset_manifest_sha256'],
        note='Original prior package/worktree paths are retained; not a relocated managed package.'))
    for source, name in ((BASE/'engine/q4t', 'q4t'),
                         (BASE/'engine/CMakeCache.txt', 'CMakeCache.txt'),
                         (BASE/'engine/compile_commands.json', 'compile_commands.json'),
                         (BASE/'engine/generated/q4t/server/chat_protocol_sources.json',
                          'chat_protocol_sources.json')):
        copy_file(source, candidate/name)
    runtime = {}
    for path, digest in plan['files'].items():
        path = Path(path)
        if not path.is_relative_to(ROOT):
            continue
        name = str(path.relative_to(ROOT))
        if name == 'CMakeLists.txt' or name.startswith(('src/', 'include/', 'cmake/')):
            h.require(h.sha(path) == digest, 'tested runtime changed: '+name)
            runtime[name] = digest
            copy_file(path, candidate/'source'/name)
    h.require(len(runtime) >= 223, 'runtime inventory incomplete')
    h.save(candidate/'runtime-source.json', runtime)
    protocol = h.read(candidate/'chat_protocol_sources.json')
    for name, digest in protocol.items():
        h.require(h.sha(ROOT/name) == digest, 'protocol source drift: '+name)
    protocol_sha = hashlib.sha256(''.join(f'{n}:{protocol[n]}\n'
        for n in sorted(protocol)).encode()).hexdigest()
    h.require(protocol_sha == result['protocol_sha256'], 'protocol identity')
    for name in ('result.json', 'originals.json', 'binding.json'):
        copy_file(BASE/'audit-03'/name, PACKAGE/'audit'/name)
    for name in ('audit-01.log', 'audit-02.log', 'audit-03.log',
                 'cpu/driver-cleanup-01.log', 'cpu/delivery-transaction-01.log'):
        copy_file(BASE/name, PACKAGE/'supplementary'/name)
    for name in ('docs/VISION_SERVICE_V1.md',
                 'docs/VISION_PRODUCTION_RESULT_2026-11-07.md',
                 'tools/vision_service/delivery.py', 'tools/vision_service/report.py',
                 'tools/vision_service/test_delivery.py',
                 'tools/examples/vision_query.py', 'tools/examples/vision_input.py',
                 'tools/examples/jetson_video.py', 'tools/examples/chat_client.py',
                 'tools/examples/deadline_http.py', 'tools/vision_ref.provenance.json'):
        copy_file(ROOT/name, PACKAGE/'source'/name)
    for path in ROOT.glob('docs/serve-*.json'):
        copy_file(path, PACKAGE/'schemas'/path.name)

    decl = candidate/'declarations'
    for source, target in (('caps-wire.bin', 'capabilities.json'),
                           ('config-wire.bin', 'configuration.json')):
        copy_file(BASE/'phase-07/execution'/source, decl/target)
        copy_file(BASE/'phase-04/execution'/source, candidate/'media-observations'/target)
    for name in declarations.DECLARATION_SCHEMAS:
        source = (ROOT/('docs/serve-'+name+'-v1.schema.json')
                  if name in ('config', 'preflight')
                  else Path(old['chat_protocol'][name+'_schema']))
        copy_file(source, decl/'schemas'/(name+'.json'))
    caps, config = (h.read(decl/name) for name in declarations.DECLARATION_FILES[:2])
    metadata = copy.deepcopy(old)
    metadata.update(version='vision-service-v1-20261107', binary_sha256=NEW,
        source_commit=plan['source_commit'], runtime_repair_commit='bdd7ce8c',
        tool_source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'],
                                                   cwd=ROOT, text=True).strip(),
        source_note='Runtime hashes equal tested phase-07; numerical repair bdd7ce8c. Later tools/docs do not alter the ELF.',
        build_cache=str(candidate/'CMakeCache.txt'),
        build_cache_sha256=h.sha(candidate/'CMakeCache.txt'),
        tested_source_manifest=str(candidate/'runtime-source.json'),
        tested_source_manifest_sha256=h.sha(candidate/'runtime-source.json'),
        acceptance='ACCEPT_SCOPED_VISION_SERVICE_V1_WITH_TASK_LIMITS',
        acceptance_decision=str(PACKAGE/'audit/result.json'),
        acceptance_decision_sha256=AUDIT,
        inherited_release=h.identity(prior/'q4t.release.json'),
        inherited_acceptance=dict(decision=old['acceptance_decision'],
                                 sha256=old['acceptance_decision_sha256']),
        previous_binary=str(prior/'q4t'), previous_release=str(prior/'q4t.release.json'),
        observed_configuration=config,
        delivery_tool=h.identity(Path(__file__)),
        scope=dict(production='Default text/MTP; explicit opt-in single-slot no-MTP image service',
            correctness='Independent HF full tower, input/reference, image tools and protocol, media client governance',
            installation_health='Exact bytes/CLI/declarations; HTTP reused from same ELF phase-07, no post-install model start',
            performance='No new optimization acceptance',
            boundaries='Video temporal enum quality and hardware decode unaccepted; no soak, real LAN, new SDK or managed/task migration'),
        vision_service_v1={key: result[key] for key in (
            'image_business_accepted', 'video_input_and_numerical_contract_accepted',
            'structured_output_syntax_accepted', 'structured_temporal_direction_accepted',
            'temporal_quality_failures', 'hardware_encoding', 'hardware_decoding',
            'long_stability', 'real_lan_deployment', 'managed_task_package_migration')})
    metadata['vision_service_v1']['profile'] = 'q4t-media-admission-v2'
    metadata['vision_service_v1']['experimental'] = True
    for key in caps.keys() & metadata['chat_protocol'].keys():
        metadata['chat_protocol'][key] = caps[key]
    metadata['chat_protocol'].update(protocol_sha256=protocol_sha, media_acceptance=False,
        declaration_evidence='phase-07 actual default text/MTP and phase-04 media observations; scoped audit retained',
        sdk_interoperability_acceptance=dict(new_sdk_qualification=False,
            current='Standard-library visual client only; old SDK/managed/task packages remain pinned'))
    for name in declarations.DECLARATION_SCHEMAS:
        path = decl/'schemas'/(name+'.json')
        metadata['chat_protocol'][name+'_schema'] = str(path)
        metadata['chat_protocol'][name+'_schema_sha256'] = h.sha(path)
    metadata['release_declarations'] = dict(profile=declarations.DECLARATION_PROFILE,
        directory=str(decl), binary_sha256=NEW, protocol_sha256=protocol_sha,
        files={name: h.sha(decl/name) for name in declarations.DECLARATION_FILES})
    h.save(candidate/'q4t.release.json', metadata)
    h.verify_pair(candidate/'q4t', candidate/'q4t.release.json', NEW)
    declarations.accepted_release(metadata, result)
    h.save(PACKAGE/'pair.json', dict(old=OLD, new=hashes(candidate),
        audit_sha256=AUDIT, original_files=len(h.read(BASE/'audit-03/originals.json')),
        runtime_sources=len(runtime), atomicity=h.ATOMICITY))
    h.seal(PACKAGE)
    return verify()


def verify():
    h.verify_seal(PACKAGE)
    pair = h.read(PACKAGE/'pair.json')
    h.require(pair['old'] == OLD and pair['new']['q4t'] == NEW, 'pair changed')
    for directory, digests in (('previous', pair['old']), ('candidate', pair['new'])):
        h.require(hashes(PACKAGE/directory) == digests, 'sealed pair differs')
        h.verify_pair(PACKAGE/directory/'q4t', PACKAGE/directory/'q4t.release.json',
                      digests['q4t'])
    state = hashes(ROOT/'build')
    h.require(all(state[n] in (pair['old'][n], pair['new'][n]) for n in NAMES),
              'unknown installed identity')
    return dict(status='PASS_PACKAGE_IDENTITIES', installed=state, pair=pair,
                mixed_pair=state not in (pair['old'], pair['new']))


@contextlib.contextmanager
def locked():
    common = Path(subprocess.check_output(['git', 'rev-parse', '--path-format=absolute',
        '--git-common-dir'], cwd=ROOT, text=True).strip())
    path = common.parent/'.q4t-work/gpu-exclusive.lock'
    h.require(not path.with_name(path.name+'.recovery-required').exists(), 'GPU recovery pending')
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        h.require(os.fstat(fd).st_ino == path.stat().st_ino, 'lock inode changed')
        yield
    finally:
        os.close(fd)


def change(rollback=False):
    with locked():
        review = verify()
        for path in (ROOT/'build').glob('*recovery*.json'):
            h.require(path == MARKER and h.read(path).get('stage') == str(PACKAGE),
                      'foreign recovery marker')
        if not rollback:
            h.require(review['installed'] == OLD and not MARKER.exists(),
                      'install requires exact previous pair and no recovery')
            OUTPUT.mkdir(mode=0o700)
        output = OUTPUT/('rollback' if rollback else 'install')
        output.mkdir(parents=True, exist_ok=False)
        if not MARKER.exists():
            h.save(MARKER, dict(stage=str(PACKAGE), action='rollback' if rollback else 'install'))
            h.fsync_directory(MARKER.parent)
        pair = review['pair']
        target = pair['old'] if rollback else pair['new']
        source = PACKAGE/('previous' if rollback else 'candidate')
        try:
            state = review['installed']
            for name in NAMES:
                if state[name] != target[name]:
                    h.replace_checked(source/name, ROOT/'build'/name, state[name])
            h.verify_pair(ROOT/'build/q4t', ROOT/'build/q4t.release.json', target['q4t'])
            # version returns before any CUDA/model setup. No serve or HTTP here.
            cli = subprocess.run([str(ROOT/'build/q4t'), 'version'], cwd=ROOT,
                capture_output=True, timeout=15, env={k:v for k,v in os.environ.items()
                    if not k.startswith('Q4T_') and k not in ('LD_PRELOAD', 'LD_LIBRARY_PATH')})
            (output/'cli.stdout').write_bytes(cli.stdout)
            (output/'cli.stderr').write_bytes(cli.stderr)
            h.require(cli.returncode == 0 and b'Qwen4-Thor' in cli.stdout, 'CLI health')
            h.require(hashes(ROOT/'build') == target, 'installed pair differs')
            h.save(output/'result.json', dict(status='PASS_ROLLBACK' if rollback else 'PASS_INSTALLED_PAIR',
                pair=target, health='CLI/identity only; same-ELF HTTP reused from phase-07',
                model_starts=0, http_requests=0, gpu_execution=False,
                persistent_service=False, atomicity=h.ATOMICITY))
            h.seal(output)
        except BaseException:
            if not rollback:
                # Only restore files whose identity is one of this transaction's
                # known values. Unknown concurrent mutation is never overwritten.
                state = verify()['installed']
                for name in NAMES:
                    if state[name] != OLD[name]:
                        h.replace_checked(PACKAGE/'previous'/name, ROOT/'build'/name, state[name])
                h.verify_pair(ROOT/'build/q4t', ROOT/'build/q4t.release.json', OLD['q4t'])
            raise  # Keep marker and partial evidence for explicit recovery.
        MARKER.unlink()
        h.fsync_directory(MARKER.parent)
        return h.read(output/'result.json')


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'verify', 'install', 'rollback'))
    action = parser.parse_args().action
    print(json.dumps(prepare() if action == 'prepare' else verify() if action == 'verify'
                     else change(rollback=action == 'rollback'), indent=2))

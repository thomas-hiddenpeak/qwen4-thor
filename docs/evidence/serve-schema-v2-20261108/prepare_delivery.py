"""A05 exact-source acceptance and immutable native package; no model launch."""
import copy
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

BASE = ROOT / '.q4t-work/serve-schema-v2-20261108'
BUILD = ROOT / '.q4t-work/serve-custom-tools-20261108/engine-build'
OUT = BASE / 'release-01'
read, save, sha, require = p.read, p.save, p.sha, p.require


def copy_file(source, dest):
    source, dest = Path(source), Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    require(not dest.exists(), 'refuse overwrite: '+str(dest))
    shutil.copyfile(source, dest)
    dest.chmod(0o500 if os.access(source, os.X_OK) else 0o400)
    require(sha(source) == sha(dest), 'copy identity')


def audit_stage(name, status, counts):
    directory = BASE / name
    result, cleanup, plan = (read(directory / file) for file in
                             ('result.json', 'cleanup.json', 'plan.json'))
    require(result['status'] == status and
            (result['counts']['starts'], result['counts']['chats']) == counts,
            'stage outcome differs: '+name)
    require(cleanup['model_exit'] == 0 and not cleanup['remaining'] and
            cleanup['ports_released'] and cleanup['lease_continuously_held'],
            'stage cleanup incomplete: '+name)
    for pid, identity in cleanup['owned'].items():
        path = Path('/proc') / pid / 'stat'
        try:
            ticks = int(path.read_text().rsplit(')', 1)[1].split()[19])
        except FileNotFoundError:
            continue
        require(ticks != identity['start_ticks'], 'owned process remains')
    lease = directory / ('lease-inherited' if name == 'business-01' else 'lease')
    end = read(lease / 'exit.json')
    require(end['status'] == ('COMMAND_FAILED' if status == 'FAILED' else 'COMPLETED')
            and end['child_returncode'] == (1 if status == 'FAILED' else 0),
            'lease outcome differs')
    samples = [json.loads(line) for line in (lease/'nvml-queries.jsonl').read_text().splitlines()]
    require(samples and all(s['status'] == 'OK' for s in samples), 'NVML abort')
    events = [json.loads(line) for line in (lease/'events.jsonl').read_text().splitlines()]
    acquired = [e['monotonic'] for e in events if e['event'] == 'lease_acquired']
    released = [e['monotonic'] for e in events if e['event'] == 'lease_released']
    require(len(acquired) == len(released) == 1, 'lease identity')
    return dict(stage=name, status=status, binary_sha256=result['binary_sha256'],
        starts=counts[0], chats=counts[1], seconds=result['seconds'],
        lease_seconds=released[0]-acquired[0], nvml_samples=len(samples),
        files={file:sha(directory/file) for file in
               ('plan.json','result.json','cleanup.json','model.log')},
        lease_exit_sha256=sha(lease/'exit.json'))


def main():
    require(not OUT.exists(), 'release already exists')
    require(not subprocess.check_output(['git','status','--porcelain']),
            'review/commit sources before packaging')
    final = read(BASE/'prompt-cache-02/plan.json')
    for name, digest in final['protected'].items():
        require(sha(name) == digest, 'default changed before delivery')
    for name, digest in final['sources'].items():
        require(sha(ROOT/name) == digest, 'tested source drift: '+name)
    require(sha(BUILD/'q4t') == final['binary_sha256'], 'accepted ELF changed')
    # Verify all frozen files against the actual committed blobs in one batch.
    names = list(final['sources'])
    raw = subprocess.check_output(['git','cat-file','--batch'], input=''.join(
        final['source_commit']+':'+n+'\n' for n in names).encode())
    offset = 0
    for name in names:
        newline = raw.index(b'\n', offset)
        header = raw[offset:newline].split()
        require(len(header) == 3 and header[1] == b'blob', 'git source missing')
        length = int(header[2]); data = raw[newline+1:newline+1+length]
        require(hashlib.sha256(data).hexdigest() == final['sources'][name],
                'committed source differs: '+name)
        offset = newline + length + 2
    stages = [audit_stage(name, status, count) for name, status, count in (
        ('business-01','FAILED',(1,5)), ('business-02','FAILED',(1,1)),
        ('business-03','PASS_SCHEMA_V2_TEXT_BUSINESS',(1,4)),
        ('media-business-01','FAILED',(1,2)), ('media-business-02','FAILED',(1,1)),
        ('media-business-03','PASS_SCHEMA_V2_MEDIA_BUSINESS',(1,3)),
        ('prompt-cache-02','PASS_SCHEMA_V2_PROMPT_CACHE',(1,3)))]
    require(all(s['binary_sha256'] == final['binary_sha256'] for s in stages[-2:]),
            'final business identities differ')
    caps, config = (read(BASE/'prompt-cache-02'/n) for n in ('capabilities.json','config.json'))
    protocol = caps['protocol_source_sha256']
    require(config['protocol_source_sha256'] == protocol and
            caps['structured_output']['schema_prompt'] == 'system_instruction_for_messages',
            'final declarations missing')
    protocol_sources = read(BUILD/'generated/q4t/server/chat_protocol_sources.json')
    digest = hashlib.sha256(''.join(n+':'+h+'\n' for n,h in
                                   sorted(protocol_sources.items())).encode()).hexdigest()
    require(digest == protocol and all(sha(ROOT/n) == h for n,h in
                                     protocol_sources.items()), 'embedded protocol source')
    old_bundle = (ROOT/'build/service').resolve(strict=True)
    old_manifest = sha(old_bundle/'manifest.json')
    bundle.verify(old_bundle, old_manifest)
    control = {n:sha(ROOT/n) for n in bundle.SOURCES}
    require(all(sha(old_bundle/'source'/n) == h for n,h in control.items()),
            'control runtime changed; review required')
    OUT.mkdir(mode=0o700)
    engine = OUT/'engine'
    engine.mkdir()
    for name in names:
        copy_file(ROOT/name, BASE/'source'/name)
    runtime = {n:sha(ROOT/n) for n in subprocess.check_output(
        ['git','ls-files','src','include','cmake','CMakeLists.txt'],text=True).splitlines()}
    save(engine/'runtime-source.json',runtime)
    for name in ('q4t','CMakeCache.txt'):
        copy_file(BUILD/name,engine/name)
    for name in ('q4t','q4t.release.json','service.release.json'):
        copy_file(ROOT/'build'/name,OUT/'previous'/name)
    declarations = engine/'declarations'
    declarations.mkdir()
    for name in ('capabilities.json','configuration.json'):
        save(declarations/name,caps if name == 'capabilities.json' else config)
    schema_paths = dict(config='serve-config-v1.schema.json',
        message='serve-message-profile-v3.schema.json',preflight='serve-preflight-v1.schema.json',
        reasoning='serve-reasoning-v1.schema.json',response='serve-transport-v1.schema.json',
        stop='serve-stop-v1.schema.json',thinking='serve-thinking-v1.schema.json')
    for name, file in schema_paths.items():
        copy_file(ROOT/'docs'/file,declarations/'schemas'/(name+'.json'))
    totals = {key:sum(s[key] for s in stages) for key in
              ('starts','chats','seconds','lease_seconds','nvml_samples')}
    decision = dict(status='PASS_TARGETED_SCHEMA_V2_DELIVERY',
        acceptance='ACCEPT_TARGETED_SCHEMA_V2', binary_sha256=final['binary_sha256'],
        protocol_sha256=protocol, source_commit=final['source_commit'],
        source_files=len(names),runtime_sources=len(runtime),protocol_sources=len(protocol_sources),
        stages=stages,totals=totals,nvml_all_ok=True,recorded_processes_exited=True,
        cpu=dict(a05_distinct_cpp=86,final_grammar_affected=26,prompt_affected=28,
                 native_journal=7,native_integration=6,media_preflights=4),
        evidence_reuse='Original text features/tool and completion/error/MTP proofs retained. '
            'Final ELF reruns all three affected text cache turns and image/tool closure. '
            'Cabin observation retained; only added model-visible format instructions changed its prompt. '
            'No arithmetic kernel/model asset changes; target state ranges are now cacheable.',
        native_binary_changed=True,native_targeted_cpu_accepted=True,
        native_gpu_restart_repeated=False,control_source_count=len(control),
        cuda_probe_reused=sha(old_bundle/'bin/q4t-cuda-probe'),
        performance_acceptance=False,full_model_quality_run=False,
        long_stability_accepted=False,remaining_master_tasks_open=True)
    save(OUT/'decision.json',decision)
    save(OUT/'unchanged-control.json',dict(previous_package=str(old_bundle),
        source_files=control,source_count=len(control),
        native_binary_changed=True,targeted_cpu=[str(BASE/'cpu-native-01/result.json'),
                                               str(BASE/'cpu-native-02/result.json')]))
    metadata = read(ROOT/'build/q4t.release.json')
    metadata.update(version='serve-schema-v2-20261108',binary_sha256=final['binary_sha256'],
        source_commit=final['source_commit'],source_note='Shared rich schema graph, exact decimals, '
        'bounded native semantic worker receipts, incremental JSON/tool streams and model-visible schemas; '
        'text target-state cache plus real image/video/tool composition. Remaining master tasks continue.',
        build_cache=str(engine/'CMakeCache.txt'),build_cache_sha256=sha(engine/'CMakeCache.txt'),
        tested_source_manifest=str(engine/'runtime-source.json'),
        tested_source_manifest_sha256=sha(engine/'runtime-source.json'),
        acceptance=decision['acceptance'],acceptance_decision=str(OUT/'decision.json'),
        acceptance_decision_sha256=sha(OUT/'decision.json'),
        inherited_release=dict(path=str(OUT/'previous/q4t.release.json'),
                               sha256=sha(OUT/'previous/q4t.release.json')),
        previous_binary=str(OUT/'previous/q4t'),previous_release=str(OUT/'previous/q4t.release.json'),
        observed_configuration=config)
    metadata['chat_protocol'].update(caps)
    metadata['chat_protocol'].update(protocol_sha256=protocol,
        constrained_stream_payload=caps['structured_output']['stream_payload'],media_acceptance=True,
        declaration_evidence='Final exact ELF observed capabilities/config; docs schemas frozen by source. '
            'Targeted text/cache and image/video/tool business accepted; all four failed attempts retained.')
    for name in schema_paths:
        file=declarations/'schemas'/(name+'.json')
        metadata['chat_protocol'][name+'_schema']=str(file)
        metadata['chat_protocol'][name+'_schema_sha256']=sha(file)
    metadata['release_declarations']=dict(profile=p.DECLARATION_PROFILE,directory=str(declarations),
        binary_sha256=final['binary_sha256'],protocol_sha256=protocol,
        files={n:sha(declarations/n) for n in p.DECLARATION_FILES})
    metadata['scope']=dict(production='Text/MTP plus opt-in image/video and structured/tool combination; '
        'target-state text cache; native ingress and Guardian retain optional TLS without nginx.',
        correctness='86 distinct A05 C++ plus client/schema/worker checks; final media and cache '
            'business plus explicit reused evidence. All failures retained.',
        performance='Functional/schema correctness work; no performance acceptance claim.',
        installation_health='Exact accepted ELF; new native JSON-link binaries have targeted CPU '
            'protocol evidence; immutable verification plus help/status, no model launch.',
        boundaries='Remaining master tasks including media state cache, persistent APIs and '
            'broader native recovery continue; user owns long stability.')
    metadata['schema_v2_acceptance']=dict(profile='q4t-json-schema-v2',
        stage=str(OUT/'decision.json'),exact_decimal=True,rich_tools=True,
        media=True,text_target_cache=True,media_state_cache=False,
        full_openai_compatibility=False)
    for key in ('session_cache','prefix_cache'):
        metadata[key]['layouts']=['mtp','target']
        metadata[key]['structured_text']=True
    notices=metadata['third_party_notices']
    notices['source_directory']=str(engine/'licenses/structured_output')
    notices['files']={}
    for file in sorted((ROOT/'third_party/licenses/structured_output').rglob('*')):
        if file.is_file():
            name=str(file.relative_to(ROOT/'third_party/licenses/structured_output'))
            copy_file(file,engine/'licenses/structured_output'/name)
            notices['files'][name]=dict(sha256=sha(file),text=file.read_text())
    idna=Path('/usr/share/doc/libidn2-0/copyright')
    copy_file(idna,engine/'licenses/system-libidn2-copyright')
    notices['optional_system_dependencies']=[dict(name='libidn2',version='2.3.7',
        linkage='dlopen system library; not redistributed',scope='isolated grammar worker hostname/email IDNA2008',
        failure='constraint_dependency_unavailable 503; healthy model retained',
        copyright_sha256=sha(idna),copyright_text=idna.read_text())]
    save(engine/'q4t.release.json',metadata)
    p.verify_declarations(metadata,required=True)
    native=dict(version=1,source_files={n:sha(ROOT/n) for n in sorted(bundle.NATIVE_SOURCES)},
        guardian_sha256=sha(BUILD/'q4t-guardian'),ingress_sha256=sha(BUILD/'q4t-ingress'),
        cuda_probe_sha256=sha(old_bundle/'bin/q4t-cuda-probe'),
        provenance=dict(build=str(BASE/'cpu-schema-prompt-01/build.log'),
            native_cpu=[str(BASE/'cpu-native-01/result.json'),str(BASE/'cpu-native-02/result.json')],
            cuda_probe_reused_from=str(old_bundle/'native-build.json'),
            cuda_probe_source_unchanged=True))
    require(native['source_files']['src/service/cuda_probe.cu'] ==
            read(old_bundle/'native-build.json')['source_files']['src/service/cuda_probe.cu'],
            'CUDA probe source changed')
    save(OUT/'native-build.json',native)
    receipt=bundle.prepare(engine/'q4t',engine/'q4t.release.json',final['model'],
        BUILD/'q4t-guardian',old_bundle/'bin/q4t-cuda-probe',None,
        old_bundle/'gateway-policy.json',old_bundle/'engine/client-policy.json',OUT/'bundle',
        native_receipt=OUT/'native-build.json',native_ingress=BUILD/'q4t-ingress')
    save(OUT/'receipt.json',receipt)
    save(BASE/'independent-review.json',dict(status='PASS_SCHEMA_V2_REVIEW',
        source_commit=final['source_commit'],source_files=len(names),committed_blobs_verified=True,
        default_unchanged=True,stages=stages,totals=totals,release=receipt))
    print(json.dumps(dict(release=receipt,totals=totals)))


if __name__=='__main__':
    main()

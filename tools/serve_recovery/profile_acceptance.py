"""Qualify exact managed primary/light capacities with actual bounded business.

Two starts, six Chat and two native embedding requests under one strict lease.
Media profiles additionally require one actual video request on each profile.
Configured TTS requires actual Speech and Chat output on both profiles.
When ASR is also configured, repeat Speech after input work to qualify reload.
Configured ASR additionally requires one actual audio request on each profile.
Configured alignment/VAD additionally requires actual auxiliary audio per profile.
Declared Chat audio requires a source-bound main-model answer on each profile.
Final qualification is constructed only after guard exit and complete cleanup.
This does not enable deployment or certify automatic managed profile switching.
"""
import argparse
import base64
import copy
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_ops import contracts
from tools.serve_recovery import bundle, profile_contract, profile_evidence, profile_voice, profile_consumer
from tools.serve_recovery import profile_realtime
from tools.serve_recovery import profile_semantic
from tools.serve_recovery import profile_conversation
from tools.serve_responses import background_acceptance as background
from tools.serve_responses import encryption_acceptance as encrypted
from tools.serve_service import capacity, package
from tools.serve_service.package import read, require, save, sha

LIMITS = dict(starts=2, chats=8, controls=2000, seconds=900)
DOCUMENT = 'docs/SERVE_MEMORY_STOP_2026-11-03.md'
SUCCESS = 'PASS_QUALIFIED_PRIMARY_LIGHT_BUSINESS'


def limits_for(profile, native_asr=None, chat_audio=False, native_tts=None,
               native_tts_base=None, *, voice_consumer=False, realtime=False,
               retained_primary_voice=False, semantic_vad=False,
               retained_primary_business=False, conversation=False):
    capacity.validate(profile)
    require(type(voice_consumer) is bool and (not voice_consumer or
        native_tts_base is not None and native_asr is None and native_tts is None),
        'saved voice qualification requires explicit Base-only backends')
    require(type(chat_audio) is bool and (not chat_audio or native_asr is not None),
            'Chat audio qualification requires native ASR')
    require(type(realtime) is bool and (not realtime or native_asr is not None),
            'Realtime qualification requires native ASR')
    require(type(conversation) is bool, 'explicit conversation checklist required')
    require(type(semantic_vad) is bool and (not semantic_vad or realtime and
        native_asr is not None and 'vad' in native_asr),
        'semantic qualification requires Realtime and native VAD')
    require(type(retained_primary_voice) is bool and
        (not retained_primary_voice or native_tts_base is not None and not voice_consumer),
        'retained voice requires its actual registration profile')
    require(type(retained_primary_business) is bool and
        (not retained_primary_business or semantic_vad and native_tts_base is not None and
         not retained_primary_voice and not voice_consumer),
        'retained primary requires complete semantic registration business')
    if native_tts_base is not None:
        package.tts_settings(native_tts_base, base=True)
        if not voice_consumer:
            require(native_asr is not None and {'speaker', 'vad'} <= native_asr.keys(),
                    'voice registration qualification requires ASR, speaker and VAD')
            package.asr_settings(native_asr, voice_registration=True)
        require(native_tts is None or package.same_json(native_tts['limits'],
                native_tts_base['limits']), 'TTS profile families must share effective limits')
    result = dict(LIMITS, seconds=800 if retained_primary_voice else LIMITS['seconds'],
                **(dict(websocket_sessions=2*realtime+4*semantic_vad+2*conversation,
                        ingress_starts=2*realtime+2*semantic_vad+2*conversation)
                   if realtime or conversation else {}),
                chats=(10 if profile.get('allow_media', False) else 8) +
                (-profile_voice.POSTS_PER_PROFILE if retained_primary_voice else 0) +
                (2*profile_realtime.POSTS_PER_PROFILE if realtime else 0) +
                (2*profile_semantic.POSTS_PER_PROFILE if semantic_vad else 0) +
                (2*profile_conversation.POSTS_PER_PROFILE if conversation else 0) +
                (2*(profile_consumer.POSTS_PER_PROFILE if voice_consumer else
                    profile_voice.POSTS_PER_PROFILE) if native_tts_base is not None else 0) +
                (2 if chat_audio else 0) + (4 if native_tts is not None else 0) +
                (2 if native_asr is not None else 0) +
                (2 if native_tts is not None and native_asr is not None else 0) +
                (2 if profile_evidence.audio_auxiliaries(dict(native_asr=native_asr or {})) else 0) +
                (2 if profile_evidence.has_speaker(dict(native_asr=native_asr or {})) else 0) +
                (2 if profile_evidence.has_separator(dict(native_asr=native_asr or {})) else 0))
    if retained_primary_business:
        result.update(starts=1, seconds=600, chats=result['chats']//2,
                      websocket_sessions=result['websocket_sessions']//2,
                      ingress_starts=result['ingress_starts']//2)
    return result



def check_tts_family_reload(before, after, previous, encoded, identity,
                            *, sampling_profile=None):
    """Require actual eviction, full byte-identical reload, and retained metadata."""
    profile_evidence.tts_audio(encoded, 'speech', sampling_profile=sampling_profile)
    old = before['native_tts_health']; new = after['native_tts_health']
    require(old.get('state') == 'metadata_ready' and old.get('device_bytes') == 0 and
        old.get('reason') == 'none' and new.get('state') == 'ready' and
        new.get('reason') == 'none' and new.get('device_bytes') == previous['device_bytes'] and
        type(new.get('load_attempts')) is int and
        new['load_attempts'] == previous['load_attempts']+1 and
        old.get('load_attempts') == previous['load_attempts'] and
        new.get('load_failures') == 0 and new.get('compute_failures') == 0,
        'TTS family did not evict and reload cleanly')
    names = ['embedding', 'asr']
    selected = identity.get('native_asr', {})
    names += [name for name in ('alignment', 'speaker', 'separator') if name in selected]
    for name in names:
        health = after['native_'+name+'_health']
        require(health.get('state') == 'metadata_ready' and
            health.get('device_bytes') == 0 and health.get('reason') == 'none' and
            health.get('core_readiness_dependency') is False,
            'input family retained device weights or lost metadata: '+name)
    if 'vad' in selected:
        require(after['native_vad_health']['state'] == 'ready',
                'CPU VAD changed during GPU family switch')


def requests(media=False):
    base = dict(model='qwen3.8-flash-next', temperature=0,
                enable_thinking=False, max_completion_tokens=256)
    schema = dict(type='object', properties=dict(answer=dict(type='integer', const=42)),
                  required=['answer'], additionalProperties=False)
    tool = dict(type='function', function=dict(name='project_document', strict=True,
        description='Read an actual project document and return its SHA256 digest.',
        parameters=dict(type='object', properties=dict(path=dict(type='string', const=DOCUMENT)),
                        required=['path'], additionalProperties=False)))
    result = dict(
        schema=dict(base, messages=[dict(role='user', content='Compute 17+25 as JSON.')],
            response_format=dict(type='json_schema', json_schema=dict(
                name='arithmetic', strict=True, schema=schema))),
        tool=dict(base, messages=[dict(role='user', content='Use project_document to read '+
            DOCUMENT+'. Then report only the first 12 hexadecimal digits of its actual SHA256.')],
            tools=[tool], tool_choice='required', parallel_tool_calls=False),
        embedding=dict(model='qwen3-embedding-0.6b', input='!',
                       dimensions=32, encoding_format='float'))
    if media:
        result['media'] = profile_evidence.media_request(
            (ROOT/profile_evidence.MEDIA_FILE).read_bytes())
    return result


def command(plan, name, directory):
    base = copy.deepcopy(plan['base'])
    base['settings'].update(profile=plan['profiles'][name], port=plan['port'],
                            operations_port=plan['operations_port'])
    if plan.get('voice_consumer'):
        base['settings']['application_state']['directory'] = str(
            directory/(name+'-application-objects'))
    args = package.command(directory, base, state_directory=directory)
    args[0] = plan['binary']
    return args


def candidate_base(base, binary_sha, metadata):
    """Use retained settings to qualify a new engine, never re-sign an old package.

    A package with configured optional backends requires their real observations.
    Those observations must be obtainable before preparing that new package.
    This private plan changes only candidate identities, not accepted settings.
    """
    require(metadata.get('binary_sha256') == binary_sha and
        type(metadata.get('chat_protocol')) is dict and
        type(metadata['chat_protocol'].get('protocol_sha256')) is str and
        bundle.ledger.HASH.fullmatch(metadata['chat_protocol']['protocol_sha256']),
        'candidate qualification release identity differs')
    result = copy.deepcopy(base)
    result['files']['q4t'] = binary_sha
    result['protocol_sha256'] = metadata['chat_protocol']['protocol_sha256']
    return result


def candidate_build_base(base, binary, directory):
    """Bind an unsealed build for qualification; never certify a release.

    Capability qualification must precede packaging. Bind current compiled
    protocol inputs and the exact binary, then require the real server's
    observations during finalization. The accepted package stays unchanged.
    """
    directory = directory.resolve(strict=True)
    require(any(directory.is_relative_to(ROOT/name) for name in ('build', '.q4t-work')),
            'candidate build must be inside an artifact directory')
    executable = (directory/'q4t').resolve(strict=True)
    require(binary.resolve(strict=True) == executable,
            'candidate build executable differs')
    source_path = directory/'generated/q4t/server/chat_protocol_sources.json'
    sources = read(source_path)
    require(type(sources) is dict and 1 <= len(sources) <= 2048,
            'compiled protocol inputs missing')
    for name, digest in sources.items():
        require(type(name) is str and not Path(name).is_absolute() and
            not any(p in ('', '.', '..') for p in name.split('/')) and
            (ROOT/name).resolve().is_relative_to(ROOT) and
            type(digest) is str and bundle.ledger.HASH.fullmatch(digest) and
            sha(ROOT/name) == digest, 'compiled protocol source differs')
    protocol = hashlib.sha256(''.join(n+':'+h+'\n' for n,h in
        sorted(sources.items())).encode()).hexdigest()
    cache = directory/'CMakeCache.txt'
    result = candidate_base(base, sha(executable), dict(binary_sha256=sha(executable),
        chat_protocol=dict(protocol_sha256=protocol)))
    identity = dict(scope='qualification_only_not_release', build=str(directory),
        compiled_sources=dict(path=str(source_path), sha256=sha(source_path)),
        build_cache=dict(path=str(cache), sha256=sha(cache)),
        accepted_settings_engine_sha256=base['files']['q4t'],
        candidate_engine_sha256=sha(executable),
        original_package_unchanged=True, new_package_qualification_pending=True)
    return result, identity


def prepare(args):
    engine = args.package.resolve()/'engine'
    base = read(engine/'manifest.json')
    manifest = read(engine.parent/'manifest.json')
    consumer_path = getattr(args, 'voice_consumer', None)
    consumer = profile_consumer.inputs(consumer_path) if consumer_path else None
    removed = {}
    if consumer is not None:
        require(not any(getattr(args, key, None) for key in
            ('asr_config', 'tts_config', 'asr_audio', 'speaker_audio',
             'separator_audio', 'chat_audio', 'tts_checkpoint_sampling', 'voice_fixtures')),
            'Base-only consumer cannot configure registration or builtin TTS inputs')
        for key in ('native_asr', 'native_tts'):
            removed[key] = base['settings'].pop(key, None)
    voice_path = getattr(args, 'tts_base_config', None)
    voice = (package.tts_settings(read(voice_path), base=True) if voice_path else
             base['settings'].get('native_tts_base'))
    if voice is not None:base['settings']['native_tts_base'] = voice
    asr_path = getattr(args, 'asr_config', None)
    asr = package.asr_settings(read(asr_path), voice_registration=voice is not None) if asr_path else \
        base['settings'].get('native_asr')
    if asr is not None:
        base['settings']['native_asr'] = asr
    tts_path = getattr(args, 'tts_config', None)
    tts = package.tts_settings(read(tts_path)) if tts_path else base['settings'].get('native_tts')
    if tts is not None:
        base['settings']['native_tts'] = tts
    checkpoint = getattr(args, 'tts_checkpoint_sampling', False)
    require(type(checkpoint) is bool and (not checkpoint or tts is not None),
            'checkpoint witness requires configured builtin TTS')
    selected_path = getattr(args, 'capacity_config', None)
    selected = capacity.normalize(read(selected_path)) if selected_path else \
        capacity.validate(base['settings']['profile'])
    profiles = profile_contract.profiles(selected)
    media = selected.get('allow_media', False)
    chat_audio = getattr(args, 'chat_audio', False)
    realtime_source = getattr(args, 'realtime_reference', None)
    conversation_source = getattr(args, 'conversation_reference', None)
    semantic_vad = getattr(args, 'semantic_vad', False)
    retained_voice = getattr(args, 'retained_primary_voice', None)
    retained_business = getattr(args, 'retained_primary_business', None)
    limits = limits_for(selected, asr, chat_audio, tts, voice,
                        voice_consumer=consumer is not None,
                        realtime=realtime_source is not None,
                        conversation=conversation_source is not None,
                        semantic_vad=semantic_vad,
                        retained_primary_business=retained_business is not None,
                        retained_primary_voice=retained_voice is not None)
    fixture_path = getattr(args, 'voice_fixtures', None)
    require((voice is not None and consumer is None) == (fixture_path is not None),
            'Base registration qualification needs its explicit voice fixture manifest')
    clips = profile_voice.fixtures(fixture_path) if fixture_path is not None else None
    if voice is not None:
        # Validate the three real client identities before any model startup.
        helper = profile_consumer if consumer is not None else profile_voice
        helper.client_policy(Stage.client_policy_base(), selected, voice)
    background.prepare(args, rows=requests(media), limits=limits, scope=__doc__)
    path = args.output.resolve()/'plan.json'
    plan = read(path)
    if realtime_source is not None:
        public_port = plan['port']+2
        require(public_port <= 65535 and public_port != plan['operations_port'],
                'Realtime public port conflicts with another listener')
        plan['realtime'] = profile_realtime.prepare(realtime_source, path.parent,
                                                  public_port, plan['sdk'])
        plan['sources'].update(plan['realtime']['files'])
        plan['sources'].update({p:sha(ROOT/p) for p in profile_realtime.SOURCES})
    if semantic_vad:
        plan['semantic_vad'] = True
        plan['sources'].update({p:sha(ROOT/p) for p in profile_semantic.SOURCES})
    if conversation_source is not None:
        public_port = plan['port']+2
        require(public_port <= 65535 and public_port != plan['operations_port'],
                'conversation public port conflicts with another listener')
        plan['conversation'] = profile_conversation.prepare(
            conversation_source, path.parent, public_port, plan['sdk'])
        plan['sources'].update(plan['conversation']['files'])
        plan['sources'].update({p:sha(ROOT/p) for p in profile_conversation.SOURCES})
    if chat_audio: plan['chat_audio'] = True
    voice_icl = getattr(args, 'voice_icl', False)
    require(type(voice_icl) is bool and (not voice_icl or
        voice is not None and consumer is None),
        'ICL qualification requires actual voice registration')
    if voice_icl: plan['voice_icl'] = True
    if checkpoint: plan['tts_sampling_profile'] = profile_evidence.TTS_CHECKPOINT_SAMPLING
    if consumer is not None:
        plan['voice_consumer'] = consumer
        plan['explicit_backend_selection'] = dict(mode='base_only_saved_voice',
            previous=removed, default_unchanged=True, registration_qualified=False)
        plan['sources'][consumer['manifest']] = consumer['sha256']
        for name, digest in consumer['files'].items():
            plan['sources'][str(Path(consumer['directory'])/name)] = digest
        source = Path(profile_consumer.__file__).resolve()
        plan['sources'][str(source)] = sha(source)
    if clips is not None:
        plan['voice_clips'] = clips
        for p in (voice_path, fixture_path):
            if p is not None:
                p = p.resolve(strict=True);plan['sources'][str(p)] = sha(p)
        for row in clips.values():plan['sources'][row['path']] = row['sha256']
        source = Path(profile_voice.__file__).resolve()
        plan['sources'][str(source)] = sha(source)
    if voice_path and consumer is not None:
        source = voice_path.resolve(strict=True)
        plan['sources'][str(source)] = sha(source)
    if tts_path:
        tts_path = tts_path.resolve(strict=True)
        plan['sources'][str(tts_path)] = sha(tts_path)
    if asr_path:
        asr_path = asr_path.resolve(strict=True)
        plan['sources'][str(asr_path)] = sha(asr_path)
    if selected_path:
        selected_path = selected_path.resolve(strict=True)
        plan['sources'][str(selected_path)] = sha(selected_path)
        plan['explicit_capacity'] = dict(path=str(selected_path),
            sha256=sha(selected_path), previous=copy.deepcopy(base['settings']['profile']),
            selected=selected, default_unchanged=True)
        base['settings']['profile'] = selected
    release = getattr(args, 'candidate_release', None)
    build = getattr(args, 'candidate_build', None)
    require(not (release and build), 'select one candidate identity source')
    if release is not None:
        release = release.resolve(strict=True)
        metadata = read(release)
        decision = Path(metadata['acceptance_decision']).resolve(strict=True)
        require(sha(decision) == metadata['acceptance_decision_sha256'],
                'candidate engine acceptance changed')
        package.accepted_release(metadata, read(decision))
        declarations = package.verify_declarations(metadata, required=True)
        for name in package.declaration_files(metadata['release_declarations']['profile']):
            plan['sources'][str(declarations/name)] = sha(declarations/name)
        plan['sources'][str(release)] = sha(release)
        plan['sources'][str(decision)] = sha(decision)
        plan['candidate_identity'] = dict(release=str(release),
            release_sha256=sha(release), accepted_settings_engine_sha256=base['files']['q4t'],
            candidate_engine_sha256=plan['binary_sha256'],
            original_package_unchanged=True, new_package_qualification_pending=True)
        base = candidate_base(base, plan['binary_sha256'], metadata)
    elif build is not None:
        base, plan['candidate_identity'] = candidate_build_base(base, args.binary, build)
        for key in ('compiled_sources', 'build_cache'):
            entry = plan['candidate_identity'][key]
            plan['sources'][entry['path']] = entry['sha256']
    else:
        require(plan['binary_sha256'] == base['files']['q4t'], 'qualification engine differs')
    require(plan['model'] == base['model_dir'], 'qualification model differs')
    require(base['settings'].get('native_embedding') is not None,
            'this acceptance requires the delivered native embedding backend')
    require(not base['settings']['application_state']['directory'],
            'qualification must use a private application directory')
    plan.update(base=base, profiles=profiles,
        identity=profile_contract.binding(base['files']['q4t'], base['protocol_sha256'],
            base['model_files'], manifest['model_assets'], base['settings']['native_embedding'],
            asr, tts, voice))
    plan['sources'][DOCUMENT] = sha(ROOT/DOCUMENT)
    if media:
        plan['sources'][profile_evidence.MEDIA_FILE] = sha(ROOT/profile_evidence.MEDIA_FILE)
    plan['sources'][str(engine/'manifest.json')] = sha(engine/'manifest.json')
    assets = Path(base['settings']['native_embedding']['directory'])
    plan['native_assets'] = {str(assets/name):sha(assets/name) for name in
                            ['config.json', 'tokenizer.json', 'model.safetensors']}
    for speech_model, fixed in ((tts, package.NATIVE_TTS),
                                (voice, package.NATIVE_TTS_BASE)):
        if speech_model is None:continue
        speech_assets = Path(speech_model['directory'])
        for prefix in ('', 'speech_tokenizer/'):
            for name in ('config.json', 'preprocessor_config.json', 'model.safetensors'):
                asset = speech_assets/(prefix+name)
                plan['native_assets'][str(asset)] = sha(asset)
        for name in ('tokenizer_config.json', 'vocab.json', 'merges.txt',
                     'generation_config.json', 'speech_tokenizer/configuration.json'):
            asset = speech_assets/name
            plan['native_assets'][str(asset)] = sha(asset)
        require(plan['native_assets'][str(speech_assets/'model.safetensors')] ==
            fixed['weight_sha256'] and
            plan['native_assets'][str(speech_assets/'speech_tokenizer/model.safetensors')] ==
            fixed['codec_weight_sha256'], 'qualified TTS paired assets differ')
    if asr is not None:
        clip = getattr(args, 'asr_audio', None)
        require(clip is not None, 'ASR qualification needs its fixed real audio')
        clip = clip.resolve(strict=True)
        require(sha(clip) == profile_evidence.ASR_FILE_SHA,
                'ASR qualification audio differs')
        plan['asr_audio'] = dict(path=str(clip), sha256=sha(clip))
        plan['sources'][str(clip)] = sha(clip)
        directory = Path(asr['directory'])
        for name in ['config.json', 'preprocessor_config.json',
                     'generation_config.json', 'vocab.json', 'merges.txt',
                     'tokenizer_config.json', 'model.safetensors']:
            asset = directory/name
            # Reading all weights belongs inside the strict stage lease.
            plan['native_assets'][str(asset)] = package.NATIVE_ASR['weight_sha256'] \
                if name == 'model.safetensors' else sha(asset)
        for auxiliary in (k for k in package.ASR_AUXILIARY if k in asr):
            settings = asr[auxiliary]
            directory = Path(settings['directory'])
            if auxiliary == 'vad':
                plan['native_assets'][str(directory/'silero_vad_16k.safetensors')] = \
                    settings['weight_sha256']
                continue
            names = ['config.json', 'model.safetensors'] if auxiliary in ('speaker','separator') else \
                ['config.json', 'preprocessor_config.json',
                         'generation_config.json', 'vocab.json', 'merges.txt',
                         'tokenizer_config.json', 'model.safetensors']
            for name in names:
                asset = directory/name
                plan['native_assets'][str(asset)] = settings['weight_sha256'] \
                    if name == 'model.safetensors' else sha(asset)
    if profile_evidence.has_speaker(plan['identity']):
        clip = getattr(args, 'speaker_audio', None)
        require(clip is not None, 'speaker qualification needs its fixed real audio')
        clip = clip.resolve(strict=True)
        require(sha(clip) == profile_evidence.SPEAKER_FILE_SHA,
                'speaker qualification audio differs')
        plan['speaker_audio'] = dict(path=str(clip), sha256=sha(clip))
        plan['sources'][str(clip)] = sha(clip)
    if profile_evidence.has_separator(plan['identity']):
        clip = getattr(args, 'separator_audio', None)
        require(clip is not None, 'separator qualification needs fixed real mixed audio')
        clip = clip.resolve(strict=True)
        require(sha(clip) == profile_evidence.SEPARATOR_FILE_SHA,
                'separator qualification audio differs')
        plan['separator_audio'] = dict(path=str(clip), sha256=sha(clip))
        plan['sources'][str(clip)] = sha(clip)
    for name in ('access-program-policy.json', 'file-input-policy.json',
                 'mcp-policy.json', 'web-policy.json'):
        if (engine/name).is_file():
            plan['sources'][str(engine/name)] = sha(engine/name)
            shutil.copyfile(engine/name, path.parent/name)
    if retained_voice is not None:
        from tools.serve_recovery import profile_voice_review
        plan['retained_primary_voice'] = profile_voice_review.prepare(retained_voice, plan)
        plan['sources'].update(plan['retained_primary_voice']['files'])
    if retained_business is not None:
        from tools.serve_recovery import profile_primary_review
        plan['retained_primary_business'] = profile_primary_review.prepare(retained_business, plan)
        plan['sources'].update(plan['retained_primary_business']['files'])
        source = Path(profile_primary_review.__file__).resolve()
        plan['sources'][str(source)] = sha(source)
    plan['planned'] = dict(starts=2, requests=limits['chats'],
                           chats=6 + 2 * chat_audio + (2 if tts is not None else 0) +
                                 (2 if voice is not None else 0),
                           native_embeddings=2, media_requests=2 if media else 0,
                           native_embedding_steps=60)
    if realtime_source is not None or conversation_source is not None:
        plan['planned'].update(realtime_sessions=limits['websocket_sessions'],
            realtime_transcriptions=6*(realtime_source is not None)+4*semantic_vad,
            realtime_chat_responses=4*(conversation_source is not None),
            native_tls_ingress_starts=limits['ingress_starts'])
    if voice is not None:
        plan['planned']['native_voice_business_requests'] = 2*(profile_consumer.POSTS_PER_PROFILE
            if consumer is not None else profile_voice.POSTS_PER_PROFILE)
        plan['planned']['native_voice_registrations'] = 0 if consumer is not None else 4
        plan['planned']['registered_voice_synthesis_requests'] = 4
        plan['planned']['registered_voice_transcriptions'] = 0 if consumer is not None else 4
        if consumer is not None:
            plan['planned']['complete_audio_witness_comparisons'] = 4
            plan['planned']['registration_not_configured_rejections'] = 4
        if retained_voice is not None:
            plan['planned']['chats'] -= 1
            plan['planned']['native_voice_business_requests'] -= profile_voice.POSTS_PER_PROFILE
            for key in ('native_voice_registrations', 'registered_voice_synthesis_requests',
                        'registered_voice_transcriptions'):
                plan['planned'][key] -= 2
            plan['planned']['reused_primary_voice_requests'] = profile_voice.POSTS_PER_PROFILE
    if tts is not None:
        plan['planned']['native_speech_requests'] = 2 + (2 if asr is not None else 0)
        plan['planned']['native_speech_reload_requests'] = 2 if asr is not None else 0
        plan['planned']['chat_audio_output_requests'] = 2
    if asr is not None:
        plan['planned']['native_transcriptions'] = 2
    if chat_audio:
        plan['planned']['chat_audio_requests'] = 2
    if profile_evidence.audio_auxiliaries(plan['identity']):
        plan['planned']['native_auxiliary_transcriptions'] = 2
    if profile_evidence.has_speaker(plan['identity']):
        plan['planned']['native_speaker_transcriptions'] = 2
    if profile_evidence.has_separator(plan['identity']):
        plan['planned']['native_separator_transcriptions'] = 2
    if retained_business is not None:
        for key in plan['planned']:
            if key not in ('requests', 'realtime_sessions', 'native_tls_ingress_starts'):
                require(plan['planned'][key] % 2 == 0, 'retained primary budget is not separable')
                plan['planned'][key] //= 2
        plan['planned']['reused_primary_requests'] = plan['retained_primary_business']['environment']['requests']
    plan['mutations'] = ['gracefully stop owned primary after its bounded business',
        'start only qualified light command in same lease/private directory',
        'default deployment and its state remain untouched']
    if retained_business is not None:
        plan['mutations'] = ['read-only reuse of completed original primary and preserved failure',
            'start only light in one new strict lease, then clean up owned processes',
            'default deployment and its state remain untouched']
    plan['derived_requests'] = ['tool results computed from actual fixed project file',
        'final conversation uses actual call ID and result, no request retries',
        'qualification only after strict guard completed and both models cleaned']
    save(path, plan)
    require(read(path) == plan, 'prepared profile plan must be readable before GPU work')


class Stage(encrypted.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = SUCCESS
    requests_per_minute = 16

    def __init__(self, plan):
        if 'tts_sampling_profile' in plan:
            profile_evidence.tts_sampling(dict(sampling_profile=plan['tts_sampling_profile']))
            require(plan['base']['settings'].get('native_tts') is not None,
                    'sampling witness without builtin TTS')
        selected = plan['profiles']['primary']
        self.limits = limits_for(selected, plan['base']['settings'].get('native_asr'),
                                 plan.get('chat_audio', False), plan['base']['settings'].get('native_tts'),
                                 plan['base']['settings'].get('native_tts_base'),
                                 voice_consumer=plan.get('voice_consumer') is not None,
                                 realtime='realtime' in plan,
                                 conversation='conversation' in plan,
                                 semantic_vad=plan.get('semantic_vad', False),
                                 retained_primary_business='retained_primary_business' in plan,
                                 retained_primary_voice='retained_primary_voice' in plan)
        self.request_templates = lambda: requests(selected.get('allow_media', False))
        super().__init__(plan)
        if 'realtime' in plan or 'conversation' in plan:
            self.counts.update(websocket_sessions=0, ingress_starts=0)

    @staticmethod
    def client_policy_base():
        # Reuse the existing private qualification policy, without an engine.
        stage = object.__new__(Stage)
        return encrypted.Stage.client_policy(stage)

    def client_policy(self):
        value = super().client_policy()
        base = self.plan['base']['settings'].get('native_tts_base')
        if base is not None:
            helper = profile_consumer if self.plan.get('voice_consumer') else profile_voice
            return helper.client_policy(value, self.plan['profiles']['primary'], base)
        tts = self.plan['base']['settings'].get('native_tts')
        if tts is not None:
            frames = tts['limits']['max_frames']
            primary = self.plan['profiles']['primary']['max_tokens']
            # Audio codes/EOS share the Chat choice budget; ordinary text's
            # old 512-token fixture must not truncate a complete utterance.
            value['clients'][0]['max_output_tokens'] = max(512, primary + frames*16+1)
        return value

    def start(self):
        offset = int('retained_primary_business' in self.plan)
        name = profile_contract.NAMES[self.counts['starts']+offset]
        self.profile_name = name
        background.probe_ports((self.plan['port'], self.plan['operations_port']))
        for path, digest in self.plan['native_assets'].items():
            require(sha(path) == digest, 'qualified native backend asset drift')
        policy = self.client_policy()
        capacity.check_clients(self.plan['profiles'][name], policy)
        save(self.out/'client-policy.json', policy)
        if self.plan.get('voice_consumer'):
            profile_consumer.clone(self.plan['voice_consumer'],
                                   self.out/(name+'-application-objects'))
        args = command(self.plan, name, self.out)
        save(self.out/(name+'-command.json'), args)
        self.budget('starts')
        with (self.out/(name+'-model.log')).open('xb', buffering=0) as log:
            self.model = subprocess.Popen(args, cwd=ROOT, stdout=log,
                stderr=subprocess.STDOUT, pass_fds=(self.lease,))
        guard = encrypted.foreground.base.lifecycle.guard
        self.model_identity = guard.processes()[self.model.pid]
        self.history[self.model.pid] = self.model_identity
        save(self.out/(name+'-identity.json'), dict(pid=self.model.pid, **self.model_identity))
        end = time.monotonic()+180
        while time.monotonic() < end:
            self.same_model()
            try:
                if self.http('/readyz', timeout=1)[0] == 200: break
            except (OSError, http.client.HTTPException): pass
            time.sleep(.1)
        else: raise TimeoutError('qualified profile readiness')
        for key in ('config', 'capabilities'):
            status, raw = self.http('/v1/'+key, label=name+'-'+key)
            require(status == 200, 'missing actual '+key)
            save(self.out/(name+'-'+key+'.json'), json.loads(raw))
        config = read(self.out/(name+'-config.json'))
        capability = read(self.out/(name+'-capabilities.json'))
        require(profile_evidence.chat_audio_enabled(self.plan['identity'], capability) is
            self.plan.get('chat_audio', False),
            'freeze the declared Chat audio business budget with --chat-audio')
        profile_contract.configuration(self.plan['profiles'][name],
                                       contracts.configuration(config, version=5))
        profile_contract.startup_memory(config['startup_memory']['checks']['before_allocations'])

    def request(self, label, body, path='/v1/chat/completions'):
        label = self.profile_name+'-'+label
        save(self.out/(label+'.request.json'), body)
        self.budget('chats')
        status, raw = self.http(path, body, label=label, timeout=120)
        require(status == 200, 'business request failed: '+label)
        result = json.loads(raw)
        self.same_model()
        self.drained(label+'-drain')
        return result

    def tts_request(self):
        rows = profile_evidence.tts_requests()
        label = self.profile_name+'-speech'
        save(self.out/(label+'.request.json'), rows['speech'])
        self.budget('chats')
        status, raw = self.http('/v1/audio/speech', rows['speech'], label=label, timeout=120)
        require(status == 200, 'qualified Speech request failed')
        self.same_model(); self.drained(label+'-drain')
        chat = self.request('chat-output', rows['chat'])
        status, config = self.http('/v1/config', label=self.profile_name+'-tts-config')
        require(status == 200, 'qualified TTS configuration unavailable')
        config = json.loads(config)
        result = dict(requests=rows, speech_base64=base64.b64encode(raw).decode(),
            chat_response=chat, health=config['native_tts_health'],
            limits=config['native_tts_limits'])
        if 'tts_sampling_profile' in self.plan:
            result['sampling_profile'] = self.plan['tts_sampling_profile']
        return result

    def tts_family_reload(self, previous):
        label = self.profile_name+'-speech-reload'
        status, raw = self.http('/v1/config', label=label+'-before')
        require(status == 200, 'pre-reload health unavailable')
        before = json.loads(raw)
        request = profile_evidence.tts_requests()['speech']
        save(self.out/(label+'.request.json'), request)
        self.budget('chats')
        status, raw = self.http('/v1/audio/speech', request, label=label, timeout=120)
        require(status == 200, 'Speech reload request failed')
        encoded = base64.b64encode(raw).decode()
        self.same_model(); self.drained(label+'-drain')
        status, raw = self.http('/v1/config', label=label+'-after')
        require(status == 200, 'post-reload health unavailable')
        after = json.loads(raw)
        check_tts_family_reload(before, after, previous['health'], encoded,
                               self.plan['identity'],
                               sampling_profile=profile_evidence.tts_sampling(previous))
        save(self.out/(label+'.result.json'), dict(status='PASS',
            speech_sha256=hashlib.sha256(base64.b64decode(encoded)).hexdigest(),
            before=before, after=after, same_model=True))

    def audio_request(self, auxiliary=False, speaker=False, separator=False):
        require(sum((auxiliary, speaker, separator)) <= 1,
                'one qualified audio request kind required')
        label = self.profile_name+('-separator' if separator else '-speaker' if speaker else
                                  '-asr-auxiliary' if auxiliary else '-asr')
        selected = self.plan['base']['settings']['native_asr']
        clip = self.plan['separator_audio' if separator else
                         'speaker_audio' if speaker else 'asr_audio']
        raw = Path(clip['path']).read_bytes()
        require(hashlib.sha256(raw).hexdigest() == clip['sha256'], 'ASR source drift')
        request = dict(model=selected['model'], response_format='json', include=['logprobs'])
        if auxiliary:
            raw = profile_evidence.auxiliary_audio_source(raw, self.plan['identity'])
            request = profile_evidence.auxiliary_audio_request(self.plan['identity'])
        if speaker:
            request = profile_evidence.speaker_request(self.plan['identity'])
        if separator:
            request = profile_evidence.separator_request(self.plan['identity'])
        boundary = 'q4t-profile-native-asr'
        parts = []
        for key, value in request.items():
            values = value if type(value) is list else [value]
            field = key+'[]' if type(value) is list else key
            for item in values:
                rendered = json.dumps(item) if type(item) is dict else str(item)
                parts.append(('--'+boundary+'\r\nContent-Disposition: form-data; name="'+
                              field+'"\r\n\r\n'+rendered+'\r\n').encode())
        parts += [('--'+boundary+'\r\nContent-Disposition: form-data; name="file"; '
                   'filename="voice.wav"\r\nContent-Type: audio/wav\r\n\r\n').encode(),
                   raw, ('\r\n--'+boundary+'--\r\n').encode()]
        self.budget('chats')
        conn = http.client.HTTPConnection('127.0.0.1', self.plan['port'], timeout=120)
        try:
            conn.request('POST', '/v1/audio/transcriptions', body=b''.join(parts),
                headers={**encrypted.HEADERS,
                         'Content-Type':'multipart/form-data; boundary='+boundary})
            response = conn.getresponse()
            wire = response.read(4*1024*1024+1)
            (self.out/(label+'.wire')).write_bytes(wire)
            save(self.out/(label+'.http.json'), dict(status=response.status,
                headers=dict(response.getheaders())))
            require(response.status == 200 and len(wire) <= 4*1024*1024,
                    'qualified native transcription failed')
        finally:
            conn.close()
        self.same_model();self.drained(label+'-drain')
        status, config = self.http('/v1/config', label=label+'-config')
        require(status == 200, 'qualified ASR configuration unavailable')
        config = json.loads(config)
        result = dict(source_base64=base64.b64encode(raw).decode(), request=request,
            response=json.loads(wire), health=config['native_asr_health'],
            limits=config['native_asr_limits'])
        if auxiliary:
            names = profile_evidence.audio_auxiliaries(self.plan['identity'])
            result['health'] = {k:config['native_'+k+'_health'] for k in names}
            result['limits'] = {k:package.asr_auxiliary_observed_limits(config, k) for k in names}
        if speaker or separator:
            del result['source_base64']
            backend = 'separator' if separator else 'speaker'
            result.update(source_sha256=hashlib.sha256(raw).hexdigest(),
                health=config['native_'+backend+'_health'],
                limits=config['native_'+backend+'_limits'])
        save(self.out/(label+'.request.json'), request)
        if separator:
            quality = profile_evidence.separator_business(result, self.plan['identity'])
            save(self.out/(label+'.quality.json'), quality)
        elif speaker:
            profile_evidence.speaker_business(result, self.plan['identity'])
        elif auxiliary:
            profile_evidence.audio_auxiliary_business(result, self.plan['identity'])
        else:
            profile_evidence.audio_business(result, self.plan['identity'])
        return result

    def profile_business(self):
        # Qualify the affected speech cases before unrelated model requests.
        capability = read(self.out/(self.profile_name+'-capabilities.json'))
        require(self.plan.get('voice_icl', False) == profile_evidence.voice_icl_enabled(
            self.plan['identity'], capability),
            'actual ICL registration needs its explicit conditioning checklist')
        require(('realtime' in self.plan) == profile_evidence.realtime_enabled(
            self.plan['identity'], capability),
            'actual Realtime needs its explicit session checklist')
        require(self.plan.get('semantic_vad', False) == profile_evidence.semantic_vad_enabled(
            self.plan['identity'], capability),
            'actual semantic VAD needs its explicit classifier/health checklist')
        require(('conversation' in self.plan) ==
                profile_evidence.conversation_enabled(capability),
                'actual native Realtime Chat needs its explicit tool checklist')
        early = {}
        if self.plan['identity'].get('native_tts_base') is not None:
            if self.profile_name == 'primary' and 'retained_primary_voice' in self.plan:
                from tools.serve_recovery import profile_voice_review
                early['voice'] = profile_voice_review.consume(self)
            else:
                helper = profile_consumer if self.plan.get('voice_consumer') else profile_voice
                early['voice'] = helper.Lifecycle(self).run()
        if profile_evidence.has_speaker(self.plan['identity']):
            early['speaker'] = self.audio_request(speaker=True)
        if profile_evidence.has_separator(self.plan['identity']):
            early['separator'] = self.audio_request(separator=True)
        rows = self.plan['requests']
        schema = self.request('schema', rows['schema'])
        call = self.request('tool', rows['tool'])
        message = profile_evidence.message(call, 'tool_calls')
        calls = message.get('tool_calls', [])
        require(len(calls) == 1 and calls[0]['function']['name'] == 'project_document' and
            json.loads(calls[0]['function']['arguments']) == dict(path=DOCUMENT),
            'unsafe or different tool request')
        source = dict(path=DOCUMENT, content=(ROOT/DOCUMENT).read_text())
        require(sha(ROOT/DOCUMENT) == self.plan['sources'][DOCUMENT], 'project document drift')
        reply = dict(role='tool', tool_call_id=calls[0]['id'], content=json.dumps(dict(
            path=DOCUMENT, sha256=sha(ROOT/DOCUMENT), answer=sha(ROOT/DOCUMENT)[:12])))
        follow = copy.deepcopy(rows['tool'])
        follow['tool_choice'] = 'none'
        follow['messages'] += [message, reply]
        result = self.request('tool-result', follow)
        embedding = self.request('embedding', rows['embedding'], '/v1/embeddings')
        value = dict(schema=schema, tool_call=call, tool_result=result,
                     tool_reply=reply, tool_source=source, embedding=embedding, **early)
        selected = self.plan['profiles'][self.profile_name]
        if selected.get('allow_media', False):
            value['media'] = dict(request=copy.deepcopy(rows['media']),
                response=self.request('media', rows['media'], '/v1/responses'))
        if self.plan['identity'].get('native_tts') is not None:
            value['tts'] = self.tts_request()
        if self.plan['identity'].get('native_asr') is not None:
            value['asr'] = self.audio_request()
        if self.plan.get('chat_audio'):
            source = value['asr']['source_base64']
            response = self.request('chat-audio', profile_evidence.chat_audio_request(source))
            status, raw = self.http('/v1/config', label=self.profile_name+'-chat-audio-config')
            require(status == 200, 'qualified Chat audio configuration unavailable')
            value['chat_audio'] = profile_evidence.chat_audio_witness(source, response, json.loads(raw))
        if profile_evidence.audio_auxiliaries(self.plan['identity']):
            value['asr_auxiliary'] = self.audio_request(auxiliary=True)
        if self.plan['identity'].get('native_tts') and self.plan['identity'].get('native_asr'):
            self.tts_family_reload(value['tts'])
        if 'realtime' in self.plan:
            value['realtime'] = profile_realtime.run(self)
        if self.plan.get('semantic_vad'):
            value['semantic_vad'] = profile_semantic.run(self)
        if 'conversation' in self.plan:
            value['conversation'] = profile_conversation.run(self)
        profile_evidence.business(value, self.plan['identity'], selected,
            read(self.out/(self.profile_name+'-capabilities.json')))
        writer = profile_semantic.save_receipt if self.plan.get('semantic_vad') else save
        writer(self.out/(self.profile_name+'-business.json'), value)

    def business(self):
        if 'retained_primary_business' in self.plan:
            from tools.serve_recovery import profile_primary_review
            profile_primary_review.verify(self.plan['retained_primary_business'])
            require(self.profile_name == 'light', 'retained primary must run only light')
            self.profile_business()
            profile_contract.capability_pair(read(self.out/'primary-capabilities.json'),
                                             read(self.out/'light-capabilities.json'))
            return
        self.profile_business()
        self.cleanup()
        first = read(self.out/'cleanup.json')
        require(first['model_exit'] == 0, 'primary did not stop normally')
        save(self.out/'primary-cleanup.json', first)
        self.model = None
        self.start()
        self.profile_business()
        profile_contract.capability_pair(read(self.out/'primary-capabilities.json'),
                                         read(self.out/'light-capabilities.json'))


def environment(exit_record, queries, cleanups):
    require(exit_record.get('status') == 'COMPLETED' and
        exit_record.get('child_returncode') == 0 and
        exit_record.get('compute_backend') == 'nvml' and
        exit_record.get('lease_inherited_by_child') is True,
        'strict GPU environment not accepted')
    require(queries and all(q.get('status') == 'OK' and q.get('returncode') == 0
                            for q in queries), 'missing or failed NVML observation')
    require(len(cleanups) == 2 and all(c.get('remaining') == [] and
        c.get('model_exit') == 0 and c.get('ports_released') is True and
        c.get('lease_continuously_held') is True for c in cleanups),
        'model cleanup not accepted')
    return dict(status='PASS', env_abort=False, all_queries_ok=True,
                nvml_queries=len(queries), cleanup_complete=True)


def finalize(out):
    plan = read(out/'plan.json')
    result = read(out/'result.json')
    limits = limits_for(plan['profiles']['primary'], plan['base']['settings'].get('native_asr'),
                         plan.get('chat_audio', False), plan['base']['settings'].get('native_tts'),
                                 plan['base']['settings'].get('native_tts_base'),
                                 voice_consumer=plan.get('voice_consumer') is not None,
                                 realtime='realtime' in plan,
                                 conversation='conversation' in plan,
                                 semantic_vad=plan.get('semantic_vad', False),
                                 retained_primary_business='retained_primary_business' in plan,
                                 retained_primary_voice='retained_primary_voice' in plan)
    require(result['status'] == SUCCESS and result['counts']['starts'] == limits['starts'] and
        result['counts']['chats'] == limits['chats'] and result['seconds'] < limits['seconds'],
        'business result or budget not accepted')
    if 'realtime' in plan or 'conversation' in plan:
        require(all(result['counts'].get(k) == limits[k]
                    for k in ('websocket_sessions', 'ingress_starts')),
                'Realtime profile sessions/entries incomplete')
    for name, digest in {**plan['sources'], **plan['protected'], **plan['native_assets']}.items():
        require(sha(ROOT/name) == digest, 'postflight identity drift: '+name)
    require(sha(plan['binary']) == plan['binary_sha256'], 'engine drift')
    bundle.check_assets(plan['model'], plan['model_assets'])
    lease = out/'lease'
    queries = [json.loads(line) for line in (lease/'nvml-queries.jsonl').read_text().splitlines()]
    cleanups = [read(out/name) for name in ('primary-cleanup.json', 'cleanup.json')]
    env = environment(read(lease/'exit.json'), queries, cleanups)
    env['lease_result_sha256'] = sha(lease/'exit.json')
    if 'retained_primary_voice' in plan:
        from tools.serve_recovery import profile_voice_review
        env = profile_voice_review.combined_environment(
            out, plan['retained_primary_voice'], env)
    if 'retained_primary_business' in plan:
        from tools.serve_recovery import profile_primary_review
        env = profile_primary_review.combined_environment(
            out, plan['retained_primary_business'], env)
    guard = encrypted.foreground.base.lifecycle.guard
    records = guard.processes()
    require(not any(records.get(int(pid), {}).get('start_ticks') == row['start_ticks']
        for c in cleanups for pid,row in c['owned'].items()), 'owned process still alive')
    background.probe_ports((plan['port'], plan['operations_port']))
    if 'realtime' in plan:
        background.probe_ports((plan['realtime']['public_port'],))
    if 'conversation' in plan:
        background.probe_ports((plan['conversation']['public_port'],))
    with guard.common_lock().open('r+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    qualified = out/'qualified'
    qualified.mkdir(mode=0o700)
    registry = dict(format=profile_contract.FORMAT, identity=plan['identity'],
                    profiles=plan['profiles'], qualifications={})
    for name in profile_contract.NAMES:
        config = read(out/(name+'-config.json'))
        capability = read(out/(name+'-capabilities.json'))
        business_value = read(out/(name+'-business.json'))
        value = dict(format=profile_evidence.format_for(plan['profiles'][name],
            plan['identity'], capability, business_value), profile=name,
            identity=plan['identity'], capacity=plan['profiles'][name],
            capabilities=capability,
            configuration=contracts.configuration(config, version=5),
            startup_memory=config['startup_memory']['checks']['before_allocations'],
            business=business_value, environment=env)
        path = qualified/(name+'.json')
        writer = profile_semantic.save_receipt if plan.get('semantic_vad') else save
        writer(path, value)
        registry['qualifications'][name] = profile_contract.qualification(name,
            value['capacity'], value['identity'], value['capabilities'],
            value['configuration'], value['startup_memory'], sha(path))
    profile_contract.validate(registry)
    for name in profile_contract.NAMES:
        path = qualified/(name+'.json')
        profile_evidence.validate(read(path), registry, name, sha(path))
    save(qualified/'registry.json', registry)
    receipt = dict(status='ACCEPT_PRIMARY_LIGHT_QUALIFICATION',
        environment=env, counts=result['counts'], seconds=result['seconds'],
        registry_sha256=sha(qualified/'registry.json'), default_unchanged=True,
        automatic_managed_switching_accepted=False, performance_claim=False)
    if 'retained_primary_voice' in plan:
        prior = plan['retained_primary_voice']['environment']
        receipt.update(reused_primary_voice=plan['retained_primary_voice']['source'],
            cumulative_starts=result['counts']['starts']+prior['model_starts'],
            cumulative_requests=result['counts']['chats']+prior['requests'],
            cumulative_seconds=result['seconds']+prior['seconds'],
            original_failure_preserved=True, separate_continuous_leases=True)
    if 'retained_primary_business' in plan:
        prior = plan['retained_primary_business']['environment']
        receipt.update(reused_primary_business=plan['retained_primary_business']['source'],
            cumulative_starts=result['counts']['starts']+prior['model_starts'],
            cumulative_requests=result['counts']['chats']+prior['requests'],
            cumulative_seconds=result['seconds']+prior['seconds'],
            original_failure_preserved=True, separate_continuous_leases=True)
    save(out/'acceptance.json', receipt)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    p = sub.add_parser('prepare')
    for name in ('binary', 'package', 'output'):
        p.add_argument('--'+name, type=Path, required=True)
    candidate = p.add_mutually_exclusive_group()
    candidate.add_argument('--candidate-release', type=Path,
                   help='accepted new engine identity with retained package settings')
    candidate.add_argument('--candidate-build', type=Path,
                   help='unsealed compiled build to qualify before release packaging')
    p.add_argument('--capacity-config', type=Path,
                   help='explicit versioned candidate capacity; retained package stays unchanged')
    p.add_argument('--tts-checkpoint-sampling', action='store_true',
                   help='use versioned complete checkpoint-sampling audio witnesses')
    p.add_argument('--tts-config', type=Path,
                   help='explicit paired TTS identity, asset directory and all budgets')
    p.add_argument('--tts-base-config', type=Path,
                   help='explicit Base identity, asset directory and shared TTS budgets')
    p.add_argument('--voice-consumer', type=Path,
                   help='explicit Base-only deployment: qualified saved voice receipt; disables ASR and builtin TTS in this private plan only')
    p.add_argument('--voice-fixtures', type=Path,
                   help='pinned consent/sample/different/negated WAV manifest; never success evidence')
    p.add_argument('--voice-icl', action='store_true',
                   help='require real persisted reference codes and full ICL input usage')
    p.add_argument('--semantic-vad', action='store_true',
                   help='Qualify actual semantic turns and native health on both profiles')
    p.add_argument('--conversation-reference', type=Path,
                   help='Qualify native text/tool Realtime without an ASR dependency')
    p.add_argument('--realtime-reference', type=Path,
                   help='qualified real history plan; freeze 3 actual WSS turns per profile')
    p.add_argument('--sdk-directory', type=Path,
                   help='explicit installed SDK matching the pinned package and lock files')
    p.add_argument('--retained-primary-voice', type=Path,
                   help='review completed original voice exchanges after the old health-name failure')
    p.add_argument('--retained-primary-business', type=Path,
                   help='review completed primary after VAD metadata-name failure; run only light')
    p.add_argument('--asr-config', type=Path,
                   help='explicit native ASR identity, asset directory and limits')
    p.add_argument('--asr-audio', type=Path,
                   help='fixed real audio required when ASR is configured')
    p.add_argument('--chat-audio', action='store_true',
                   help='freeze one actual Chat speech request on each profile')
    p.add_argument('--speaker-audio', type=Path,
                   help='fixed returning-speaker audio required when speaker is configured')
    p.add_argument('--separator-audio', type=Path,
                   help='fixed real mixed audio required when separator is configured')
    p.add_argument('--port', type=int, default=19069)
    p = sub.add_parser('run'); p.add_argument('--plan', type=Path, required=True)
    p = sub.add_parser('finalize'); p.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'prepare': prepare(args)
    elif args.mode == 'finalize': finalize(args.output.resolve())
    else:
        plan = read(args.plan)
        def timeout(_signal, _frame): raise TimeoutError('profile qualification deadline')
        prior = signal.signal(signal.SIGALRM, timeout)
        try:
            signal.setitimer(signal.ITIMER_REAL, plan['limits']['seconds']-40)
            with encrypted.foreground.base.lifecycle.child_subreaper():
                Stage(plan).run()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, prior)


if __name__ == '__main__': main()

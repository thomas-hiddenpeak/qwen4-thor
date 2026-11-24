"""Qualified version/capacity migration without changing historical budgets.

Only a STOPPED deployment owner or the independent native version root after
whole-tree retirement may construct the authorized store contract, using both
sealed packages and its durable deployment/version intent. Ordinary
runtime profile workers retain profile_policy and cannot authorize migration.
Explicit capacity changes retain historical qualifications in state v2. Auto
fallback remains capacity-preserving and never changes the user's contract.
"""
import copy

from tools.serve_recovery import ledger, profile_contract, profile_evidence
from tools.serve_recovery import profile_policy as policy
from tools.serve_service import package

FORMAT = 'q4t-profile-migration-permit-v1'
CAPACITY_FORMAT = 'q4t-profile-migration-permit-v2'
EXTENSION_FORMAT = 'q4t-profile-migration-permit-v3'
BACKEND_CAPACITY_FORMAT = 'q4t-profile-migration-permit-v4'
AUDIO_EXTENSION_FORMAT = 'q4t-profile-migration-permit-v5'
TTS_CAPACITY_FORMAT = 'q4t-profile-migration-permit-v6'
VOICE_FORMAT = 'q4t-profile-migration-permit-v7'
RESPONSE_OUTPUT_TYPES = frozenset(('message', 'reasoning', 'function_call',
    'custom_tool_call', 'compaction', 'file_search_call', 'web_search_call', 'tool_search_call',
    'tool_search_output', 'program', 'program_output', 'mcp_list_tools',
    'mcp_approval_request', 'mcp_call'))
MCP_PROVIDER_IDS = frozenset(('connector_dropbox', 'connector_gmail',
    'connector_googlecalendar', 'connector_googledrive', 'connector_microsoftteams',
    'connector_outlookcalendar', 'connector_outlookemail', 'connector_sharepoint'))
CHAT_EFFORTS = frozenset(('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'))
LEGACY_CHAT_EFFORTS = frozenset(('low', 'medium', 'xhigh'))


def web_search_capabilities(value):
    """A standard item alone does not prove native execution or isolation."""
    responses = value.get('responses')
    response = responses if type(responses) is dict else {}
    items = response.get('output_items', [])
    chat, web = value.get('web_search'), response.get('web_search')
    declared = type(items) is list and 'web_search_call' in items
    if chat is None and web is None and not declared:
        return
    ledger.require(type(items) is list and 'web_search_call' in items and
        type(chat) is dict and type(web) is dict and
        chat.get('profile') == web.get('profile') == 'q4t-native-web-search-v1',
        'web search output lacks its complete native implementation')
    chat_values = dict(chat_options='web_search_options',
        policy_profile='q4t-web-deployment-policy-v1',
        configuration_endpoint='/v1/config', configuration_field='web_deployment')
    web_values = dict(executor='native_configured_sites',
        actions=['search', 'open_page', 'find_in_page'],
        filters=['allowed_domains', 'blocked_domains'],
        search_content_types=['text', 'image'],
        source_include='web_search_call.action.sources',
        citation='actual_source_marker_in_generated_text',
        citation_offsets='unicode_scalar', runtime_status='/v1/config.web_deployment')
    ledger.require(all(chat.get(k) == v for k, v in chat_values.items()) and
        all(web.get(k) == v for k, v in web_values.items()),
        'invalid native web source or citation contract')
    for object_, enabled, disabled in (
        (chat, ('pre_answer_search', 'json', 'stream', 'url_citations',
                'stored', 'batch_frozen_inputs', 'policy_check_cli'),
               ('external_program_required', 'request_override',
                'core_readiness_dependency', 'model_restart_on_error')),
        (web, ('implemented', 'offline_index', 'user_location', 'search_context_size',
               'image_settings', 'return_token_budget', 'max_tool_calls',
               'shared_output_budget', 'shared_acquisition_budget', 'private_history',
               'background', 'batch'),
              ('history_replays_search', 'core_readiness_dependency',
               'model_restart_on_error'))):
        ledger.require(all(object_.get(k) is True for k in enabled) and
            all(object_.get(k) is False for k in disabled),
            'incomplete native web execution or isolation contract')


def text_stop_capabilities(value):
    """Recognize the complete visible-output and prompt-snapshot extension."""
    stop = value.get('text_stop')
    if not isinstance(stop, dict) or stop.get('profile') not in (
            'q4t-text-stop-v1', 'q4t-text-stop-v2'):
        return
    modern = stop['profile'] == 'q4t-text-stop-v2'
    contract = dict(
        constrained_terminal='validate_visible_complete_values_and_receipts',
        private_cache_boundary='complete_prompt_before_generation')
    structured = value.get('structured_output')
    ledger.require(type(stop.get('constrained_output')) is bool and
                   type(stop.get('state_cache')) is bool,
                   'invalid text stop capability booleans')
    if modern:
        ledger.require(stop['state_cache'] and type(structured) is dict and
                       type(structured.get('available')) is bool and
                       stop['constrained_output'] == structured['available'] and
                       all(stop.get(k) == v for k, v in contract.items()),
                       'incomplete text stop combination contract')
    else:
        ledger.require(not stop['state_cache'] and not stop['constrained_output']
                       and not any(k in stop for k in contract),
                       'legacy text stop cannot declare new semantics')
    stop['profile'] = 'q4t-text-stop'
    for key in ('constrained_output', 'state_cache'):
        stop[key] = dict(enabled=True) if stop[key] else {}


def media_cache_capabilities(value, media):
    """Normalize only the qualified complete-media-cache implementation.

    The pending markers are negative capability declarations. Completing this
    exact contract adds a feature; generic boolean flips remain replacements.
    Both protocol observations and the state/position contracts must agree.
    """
    if not any(key in media for key in ('state_cache', 'chunked_prefill')):
        return
    state, chunk = media.get('state_cache'), media.get('chunked_prefill')
    ledger.require(type(state) is bool and type(chunk) is bool and state == chunk,
                   'inconsistent media cache/prefill implementation')
    contract = dict(cache_identity='source_bytes_processor_geometry_and_position',
                    cache_layout='target_with_three_axis_rope_and_delta',
                    cache_boundaries='complete_visual_blocks')
    ledger.require(all(media.get(k) == v for k, v in contract.items()) if state else
                   not any(k in media for k in contract),
                   'invalid complete media cache contract')
    for name in ('chat', 'responses'):
        protocol = value.get(name)
        ledger.require(protocol is None or type(protocol) is dict,
                       'invalid protocol capability object')
        cache = protocol.get('prompt_cache') if protocol is not None else None
        ledger.require(cache is None or type(cache) is dict,
                       'invalid prompt cache capability object')
        if not state and (cache is None or 'media_fingerprint_pending' not in cache):
            continue  # Older observations did not declare this capability.
        ledger.require(type(cache) is dict and
                       cache.get('media_fingerprint_pending') is (not state),
                       'protocol media fingerprint contract differs')
        cache['media_fingerprint_pending'] = dict(implemented=True) if state else {}
    media['state_cache'] = dict(enabled=True) if state else {}
    media['chunked_prefill'] = dict(enabled=True) if chunk else {}


def media_capabilities(value):
    """Represent reviewed optional-media activation as feature additions.

    Disabling media, lowering/changing its observed device limit, or altering
    any other media contract still fails extension. Automatic migration still
    requires exact equivalence. Never discard the original observations.
    """
    runtime, media = value.get('runtime'), value.get('media')
    if not isinstance(media, dict) or media.get('profile') != 'q4t-media-admission-v3':
        return
    media_cache_capabilities(value, media)
    ledger.require(type(runtime) is dict and type(runtime.get('media_enabled')) is bool,
                   'missing optional media runtime observation')
    enabled = runtime['media_enabled']
    shared = media.get('attention_shared_bytes')
    ledger.require((enabled and ledger.integer(shared, minimum=1)) or
                   (not enabled and shared is None), 'invalid media device observation')
    runtime['media_enabled'] = dict(enabled=True) if enabled else {}
    media['attention_shared_bytes'] = {str(shared): True} if enabled else {}
    video = value.get('file_inputs', {}).get('native_video')
    if video is not None:
        ledger.require(type(video) is dict and
            video.get('profile') == 'q4t-native-video-input-v1' and
            video.get('model_media_enabled') is enabled and
            type(video.get('requires_mtp_disabled')) is bool,
            'invalid native video media observation')
        video['model_media_enabled'] = dict(enabled=True) if enabled else {}
        video['requires_mtp_disabled'] = {} if video['requires_mtp_disabled'] else \
            dict(joint_text_mtp=True)


def tts_capabilities(value):
    speech, chat = value.get('speech'), value.get('chat_audio_output')
    if speech is None and chat is None:
        return
    ledger.require(type(speech) is dict and type(chat) is dict and
        type(speech.get('enabled')) is bool and
        chat.get('enabled') is speech['enabled'], 'invalid TTS activation capability')
    enabled = speech['enabled']
    base = speech.get('registered_custom_voices', False)
    builtin = speech.get('builtin_enabled', enabled and not base)
    ledger.require(type(base) is bool and type(builtin) is bool,
                   'invalid TTS family activation')
    transcription = value.get('transcriptions', {})
    asr = ({name: {} for name in package.ASR_AUXILIARY
            if transcription.get(name, {}).get('configured') is True}
           if transcription.get('configured') is True else None)
    profile_contract.tts_capability_binding(
        package.NATIVE_TTS if builtin else None, value,
        package.NATIVE_TTS_BASE if base else None, asr)
    # The root model selects an active family. Normalize that selection
    # into a set of fixed qualified families, so adding Base does not replace
    # a previously accepted builtin family (or an inactive model declaration).
    speech['qualified_families'] = {}
    for name, active, fixed in [('builtin', builtin, package.NATIVE_TTS),
                                 ('base', base, package.NATIVE_TTS_BASE)]:
        if active:
            speech['qualified_families'][name] = dict(fixed)
    for key in package.NATIVE_TTS:
        speech.pop(key, None)
    for key in ('registered_custom_voices', 'builtin_enabled'):
        if key in speech:
            speech[key] = dict(enabled=True) if speech[key] else {}
    registration = value.get('voice_registration')
    if registration is not None:
        ledger.require(type(registration.get('enabled')) is bool,
                       'invalid voice registration activation')
        registration['enabled'] = dict(enabled=True) if registration['enabled'] else {}
    for item in (speech, chat):
        item['enabled'] = dict(enabled=True) if enabled else {}


def tts_capacity_change(before, after, *, base=False):
    profile_contract.tts_configuration(before, base=base)
    profile_contract.tts_configuration(after, base=base)
    ledger.require(all(before[k] == after[k] for k in package.NATIVE_TTS),
                   'migration would change qualified talker/codec identity')
    return before['limits'] != after['limits']


def asr_capabilities(value):
    """Optional activation adds a qualified backend, never removes one."""
    selected = value.get('transcriptions')
    if selected is None:
        return
    profile_evidence.chat_audio_capability(selected)
    ledger.require(type(selected) is dict and
        all(selected.get(k) == v for k, v in package.NATIVE_ASR.items()) and
        type(selected.get('configured')) is bool,
        'invalid native ASR capability identity')
    selected['configured'] = dict(enabled=True) if selected['configured'] else {}
    if 'formats' in selected:
        formats = selected['formats']
        basic, aligned = {'json', 'text'}, {'srt', 'verbose_json', 'vtt'}
        ledger.require(type(formats) is list and all(type(v) is str for v in formats)
            and len(set(formats)) == len(formats) and basic <= set(formats)
            and set(formats) <= basic | aligned | {'diarized_json'},
            'invalid native ASR format capabilities')
        if set(formats) & aligned:
            ledger.require(aligned <= set(formats) and 'alignment' in selected and
                type(selected.get('segments')) is dict and
                selected['segments'].get('scope') == 'independently_decoded_audio_clip',
                'aligned formats lack complete backend/segment implementation')
        # The API's output format enum is an unordered feature set. Only this
        # reviewed enum may extend; arbitrary arrays still require equality.
        selected['formats'] = {name:True for name in formats}
    if 'stream_formats' in selected:
        formats = selected['stream_formats']
        ledger.require(type(formats) is list and all(type(v) is str for v in formats)
            and len(set(formats)) == len(formats) and {'json', 'text'} <= set(formats)
            and set(formats) <= {'json', 'text', 'diarized_json'},
            'invalid native ASR stream formats')
        selected['stream_formats'] = {name:True for name in formats}
    diarized = any('diarized_json' in selected.get(k, {})
                   for k in ('formats', 'stream_formats'))
    if diarized:
        speaker = selected.get('speaker', {})
        ledger.require(type(speaker) is dict and
            all('diarized_json' in selected.get(k, {}) for k in ('formats', 'stream_formats')) and
            all(speaker.get(k) == v for k,v in package.NATIVE_SPEAKER.items()) and
            {'alignment', 'vad', 'speaker'} <= selected.keys() and
            speaker.get('backend') == 'native_ecapa_fp32' and
            speaker.get('word_assignment') == 'actual_alignment_maximum_overlap' and
            speaker.get('reference_scope') == 'current_request_only' and
            speaker.get('core_readiness_dependency') is False,
            'diarized formats lack actual isolated speaker implementation')
    for name, (fixed, _, _) in package.ASR_AUXILIARY.items():
        if name not in selected:
            continue
        observed = selected[name]
        ledger.require(type(observed) is dict and
            all(observed.get(k) == v for k,v in fixed.items()) and
            type(observed.get('configured')) is bool,
            'invalid native auxiliary audio capability')
        observed['configured'] = dict(enabled=True) if observed['configured'] else {}
        if name == 'vad':
            ledger.require(ledger.integer(observed.get('max_chunks'), minimum=1)
                and observed['max_chunks'] <= 256, 'invalid VAD declared capacity')
            # This budget remains bound and checked in the immutable qualified
            # identity. Explicit budget migration must not be mistaken for a
            # change of the backend's features, or silently ignored on fallback.
            del observed['max_chunks']
    if 'remaining_native_work' in selected:
        pending = selected.pop('remaining_native_work')
        known = {'alignment', 'alignment_segments', 'vad_chunking',
                 'diarization', 'overlap_diarization'}
        ledger.require(type(pending) is list and all(type(k) is str for k in pending)
            and len(set(pending)) == len(pending) and set(pending) <= known and
            not {'diarization', 'overlap_diarization'} <= set(pending),
            'unknown native audio completion declaration')
        resolved = {}
        if not {'alignment', 'alignment_segments'}.intersection(pending):
            ledger.require('alignment' in selected and 'segments' in selected,
                           'alignment completion lacks actual backend/segment declaration')
            resolved['alignment'] = True
        if 'vad_chunking' not in pending:
            ledger.require('vad' in selected and selected.get('chunking_strategy') == ['auto', 'server_vad'],
                           'VAD completion lacks actual backend/strategy declaration')
            resolved['vad_chunking'] = True
        if 'diarization' not in pending:
            ledger.require(diarized, 'diarization completion lacks actual output/backend')
            resolved['diarization'] = True
            if 'overlap_diarization' not in pending:
                separator = selected.get('separator', {})
                ledger.require(type(separator) is dict and
                    all(separator.get(k) == v for k,v in package.NATIVE_SEPARATOR.items()) and
                    {'alignment','vad','speaker'} <= selected.keys() and
                    separator.get('backend') == 'native_sepformer_fp32' and
                    separator.get('simultaneous_sources') == 2 and
                    separator.get('sample_rate') == 8000 and
                    separator.get('output_sample_rate') == 16000 and
                    separator.get('speaker_identity') == 'cross_track_acoustic_clustering' and
                    separator.get('timestamps') == 'original_time_with_overlap' and
                    separator.get('budget') == 'shared_request' and
                    separator.get('core_readiness_dependency') is False,
                    'overlap completion lacks actual isolated separator')
                resolved['overlap_diarization'] = True
        selected['resolved_native_work'] = resolved


def audio_change(before, after, *, before_voice=False, after_voice=False):
    """Only addition and explicit capacity changes preserve accepted backends."""
    profile_contract.asr_configuration(before, voice_registration=before_voice)
    profile_contract.asr_configuration(after, voice_registration=after_voice)
    old = set(package.ASR_AUXILIARY).intersection(before)
    new = set(package.ASR_AUXILIARY).intersection(after)
    ledger.require(old <= new and all(before[k] == after[k] for k in package.NATIVE_ASR),
                   'migration would remove or change an accepted audio backend')
    for name in old:
        ledger.require(all(before[name][k] == after[name][k] for k in package.ASR_AUXILIARY[name][0]),
                       'migration would change fixed audio auxiliary identity')
    capacity = before['limits'] != after['limits'] or any(
        before[name]['limits'] != after[name]['limits'] for name in old)
    return bool(new-old), capacity


def realtime_pending_capabilities(value):
    """Normalize only the reviewed semantic-VAD todo transition.

    The actual semantic declaration and its qualified business stay intact.
    Other pending features cannot disappear through this metadata adapter.
    Raw declarations still bind permit hashes and historical receipts.
    """
    realtime = value.get('realtime')
    if type(realtime) is not dict or 'pending_execution' not in realtime:
        return
    semantic = realtime.get('semantic_vad')
    declared = 'semantic_vad' in realtime
    ledger.require(not declared or (type(semantic) is dict and
        semantic.get('implemented') is True and
        semantic.get('profile') == 'q4t-native-semantic-turn-v1'),
        'pending Realtime metadata lacks semantic implementation')
    if 'live' in value or 'calls' in realtime or 'client_secrets' in realtime:
        live, calls, secrets = value.get('live'), realtime.get('calls'), realtime.get('client_secrets')
        ledger.require(declared and type(realtime.get('conversation')) is dict
            and realtime['conversation'].get('implemented') is True
            and type(live) is dict and live.get('sessions') == '/v1/live/sessions'
            and live.get('transports') == ['websocket', 'webrtc', 'sip']
            and live.get('media_readiness_dependency') is False and
            type(calls) is dict and calls.get('create') == '/v1/realtime/calls'
            and type(secrets) is dict and secrets.get('issue') == '/v1/realtime/client_secrets'
            and secrets.get('owner_scoped') is True and
            realtime['pending_execution'] == [], 'pending Realtime media declarations differ')
        # Preserve the supplied capabilities in the permit identity. Historical
        # profiles below retain the original compatibility normalization.
        return
    expected = ['conversation', 'live']
    ledger.require(realtime['pending_execution'] ==
        (expected if declared else ['semantic_vad', *expected]),
        'pending Realtime metadata contradicts qualified capabilities')
    realtime['pending_execution'] = expected


def realtime_prompt_capabilities(value):
    """Allow only the reviewed v1-to-v2 addition on explicit qualification.

    Keep the real Prompt declaration in the semantic contract. Normalizing
    its old pending label and the protocol revision must never make automatic
    recovery to a profile without Prompt appear equivalent.
    """
    if not profile_evidence.conversation_enabled(value):
        return
    chat = value['realtime']['conversation']
    audio = chat.get('input_audio')
    if audio is not None and audio['configured']:
        # The validated total comprises the unchanged base plus this feature's
        # explicit reservation. Keep that increment in input_audio so neither
        # automatic fallback nor a capability extension can hide its cost.
        chat['session_host_reservation_bytes'] -= audio['additional_host_reservation_bytes']
    chat['profile'] = 'q4t-native-realtime-chat-v1'
    # A reviewed output implementation replaces only its old pending label.
    # Keep speech_output and audio modality: automatic fallback still needs
    # exact capabilities and cannot select the old text-only program.
    chat['pending_execution'] = ['audio', 'mcp', 'tracing']
    chat['output_modalities'] = {kind: True for kind in chat['output_modalities']}


def semantic_capabilities(value):
    """Separate reviewed implementation identities from promised behavior.

    The full original observations remain in the sealed registry/evidence.
    v4 adds model refusal framing to the unchanged v3 message input schema;
    refusal itself remains a capability and cannot disappear on recovery.
    Renderer and tool-history fingerprints identify code, not capabilities;
    the original hashes stay bound in each sealed qualification. Unknown
    template revisions and other changed values are not normalized.
    """
    result = copy.deepcopy(value)
    text_stop_capabilities(result)
    media_capabilities(result)
    web_search_capabilities(result)
    tts_capabilities(result)
    asr_capabilities(result)
    realtime_pending_capabilities(result)
    realtime_prompt_capabilities(result)
    responses = result.get('responses')
    if type(responses) is dict and 'output_items' in responses:
        items = responses['output_items']
        ledger.require(type(items) is list and all(type(x) is str for x in items)
            and len(set(items)) == len(items) and
            set(items) <= RESPONSE_OUTPUT_TYPES, 'invalid response output capabilities')
        if 'file_search_call' in items:
            search = responses.get('file_search')
            ledger.require(type(search) is dict and
                search.get('profile') == 'q4t-file-search-v1' and
                search.get('implemented') is True,
                'file search output lacks its implementation capability')
        discovery_items = {'tool_search_call', 'tool_search_output'}
        if discovery_items.intersection(items):
            discovery = responses.get('tool_discovery')
            ledger.require(discovery_items.issubset(items) and
                type(discovery) is dict and
                discovery.get('profile') == 'q4t-tool-discovery-v1' and
                discovery.get('definitions') == 'ordered_history' and
                all(discovery.get(key) is True for key in ('defer_loading',
                    'server_paths', 'client_search', 'additional_tools')),
                'tool discovery outputs lack their implementation capability')
        program_items = {'program', 'program_output'}
        if program_items.intersection(items):
            program = responses.get('programmatic_tools')
            ledger.require(program_items.issubset(items) and
                type(program) is dict and
                program.get('profile') == 'q4t-programmatic-tools-v1' and
                program.get('caller_binding') == 'program_call_id' and
                program.get('schema_binding') == 'invocation_time' and
                all(program.get(key) is True for key in ('enabled',
                    'allowed_callers', 'authenticated_continuation',
                    'isolated_worker', 'loaded_tools_only')) and
                program.get('automatic_tool_replay') is False and
                program.get('model_restart_on_error') is False,
                'program outputs lack their implementation capability')
        mcp_items = {'mcp_list_tools', 'mcp_approval_request', 'mcp_call'}
        if mcp_items.intersection(items):
            mcp = responses.get('mcp')
            ledger.require(mcp_items.issubset(items) and type(mcp) is dict and
                mcp.get('profile') == 'q4t-native-mcp-v1' and
                mcp.get('approval_default') == 'always' and
                all(mcp.get(key) is True for key in ('enabled',
                    'authenticated_continuation', 'catalog_filtering',
                    'input_output_schemas', 'content_and_resources',
                    'frozen_media_replay', 'encrypted_session_state')) and
                all(mcp.get(key) is False for key in ('automatic_tool_replay',
                    'model_restart_on_error', 'core_readiness_dependency')),
                'MCP outputs lack their implementation capability')
            if any(key in mcp for key in ('native_provider_adapters',
                    'native_provider_ids', 'connector_transport')):
                enabled = mcp.get('native_provider_adapters')
                providers = mcp.get('native_provider_ids', [])
                ledger.require(type(enabled) is bool and type(providers) is list
                    and all(type(item) is str for item in providers)
                    and len(set(providers)) == len(providers)
                    and set(providers) <= MCP_PROVIDER_IDS
                    and enabled == bool(providers)
                    and mcp.get('connector_transport') == (
                        'native_or_deployment_mapped_mcp' if enabled else
                        'deployment_mapped_mcp'),
                    'invalid native MCP provider capabilities')
                # Only this reviewed provider enum can extend. Automatic
                # recovery still requires equality, never provider loss/addition.
                mcp['native_provider_adapters'] = {item: True for item in providers}
                mcp.pop('native_provider_ids', None)
                mcp['connector_transport'] = dict(deployment_mapped_mcp=True,
                    **(dict(native_provider=True) if enabled else {}))
        # This field declares an unordered set of output types. Normalize only
        # this reviewed enum; all other arrays retain exact-value comparison.
        responses['output_items'] = {item: True for item in items}
    template = result.get('template')
    if isinstance(template, dict):
        if 'reasoning_effort' in template:
            efforts = template['reasoning_effort']
            ledger.require(type(efforts) is list and efforts and
                all(type(item) is str for item in efforts) and
                len(set(efforts)) == len(efforts) and set(efforts) <= CHAT_EFFORTS,
                'invalid Chat reasoning effort capabilities')
            if not set(efforts) <= LEGACY_CHAT_EFFORTS:
                ledger.require(template.get('effort_adaptation') == 'native_guidance' and
                    template.get('reasoning_effort_default') == 'medium' and
                    template.get('none_disables_thinking') is True,
                    'Chat reasoning effort lacks native implementation capability')
            # Only this reviewed enum is a set. Defaults and all other values
            # remain part of the contract; automatic recovery requires equality.
            template['reasoning_effort'] = {item: True for item in efforts}
        for key in ('renderer_source_sha256', 'tool_history_source_sha256'):
            if key in template:
                fingerprint = template.pop(key)
                ledger.require(type(fingerprint) is str and
                    ledger.HASH.fullmatch(fingerprint),
                    'invalid template source identity: '+key)
        if template.get('message_profile') == 'qwen3.8-openai-v4':
            refusal = result.get('chat', {}).get('refusal_output', {})
            ledger.require(type(refusal) is dict and
                refusal.get('profile') == 'q4t-refusal-v1',
                'v4 template lacks its refusal capability')
            template['message_profile'] = 'qwen3.8-openai-v3'
    return result


def extends(previous, target):
    """Object additions only; deletion, replacement and type coercion fail."""
    if type(previous) is not type(target):
        return False
    if type(previous) is dict:
        return all(key in target and extends(value, target[key])
                   for key, value in previous.items())
    return previous == target


def explicit_extension(previous, target):
    """Compare qualified semantics when a declared pending capability ships."""
    if 'hosted_python' not in previous and 'hosted_python' in target:
        from tools.serve_recovery import profile_live_reuse as live
        if (not package.same_json(target['hosted_python'], live.CODE_CAPABILITY) or
                not package.same_json(previous.get('container_workspaces'), live.SKILLS_CAPABILITY)):
            return False
    if 'container_workspaces' not in previous and 'container_workspaces' in target:
        from tools.serve_recovery import profile_live_reuse as live
        if (not package.same_json(target['container_workspaces'], live.SKILLS_CAPABILITY) or
                not package.same_json(previous.get('assistants'), live.ASSISTANTS_CAPABILITY)):
            return False
    if 'assistants' not in previous and 'assistants' in target:
        from tools.serve_recovery import profile_live_reuse as live
        if (not package.same_json(target['assistants'], live.ASSISTANTS_CAPABILITY) or
                not package.same_json(previous.get('translations'), live.TRANSLATION_CAPABILITY)):
            return False
    if 'translations' not in previous and 'translations' in target:
        from tools.serve_recovery import profile_live_reuse as live
        if not package.same_json(target['translations'], live.TRANSLATION_CAPABILITY):
            return False
    if 'agent_session_subagents' not in previous and 'agent_session_subagents' in target:
        from tools.serve_recovery import profile_live_reuse as live
        if (not package.same_json(target['agent_session_subagents'], live.SUBAGENT_CAPABILITY) or
                any(not package.same_json(contract.get('agent_sessions'), live.MANAGED_TOOL_SESSION)
                    or not package.same_json(contract.get('agent_session_artifacts'), live.ARTIFACT_CAPABILITY)
                    for contract in (previous, target))):
            return False
    requested = target.get('agent_sessions')
    if (isinstance(requested, dict) and 'time_awareness' in requested and
            not package.same_json(previous.get('agent_sessions'), requested)):
        from tools.serve_recovery import profile_live_reuse as live
        if (not package.same_json(previous.get('agent_sessions'), live.WORKSPACE_SESSION) or
                not package.same_json(requested, live.MANAGED_TOOL_SESSION) or
                any(not package.same_json(contract.get('agent_session_artifacts'), live.ARTIFACT_CAPABILITY)
                    or not package.same_json(contract.get('agent_environments'), live.WORKSPACE_CAPABILITY)
                    for contract in (previous, target))):
            return False
        previous = dict(previous, agent_sessions=requested)
    if 'agent_session_artifacts' not in previous and 'agent_session_artifacts' in target:
        from tools.serve_recovery import profile_live_reuse as live
        if (not package.same_json(previous.get('agent_environments'), live.WORKSPACE_CAPABILITY) or
                not package.same_json(previous.get('agent_sessions'), live.WORKSPACE_SESSION) or
                not package.same_json(target['agent_session_artifacts'], live.ARTIFACT_CAPABILITY)):
            return False
    if 'agent_environments' not in previous and 'agent_environments' in target:
        from tools.serve_recovery import profile_live_reuse as live
        if (package.same_json(previous.get('agent_sessions'), live.OAUTH_SESSION)
                and package.same_json(target.get('agent_sessions'), live.WORKSPACE_SESSION)
                and package.same_json(target.get('agent_environments'), live.WORKSPACE_CAPABILITY)
                and package.same_json(previous.get('agent_environment_templates'), live.TEMPLATE_CAPABILITY)
                and package.same_json(previous.get('agent_session_traces'), live.SESSION_TRACE_CAPABILITY)):
            previous = dict(previous, agent_sessions=live.WORKSPACE_SESSION,
                            agent_environments=live.WORKSPACE_CAPABILITY)
    if 'agent_vaults' in previous and 'agent_vaults' in target:
        from tools.serve_recovery import profile_live_reuse as live
        if (package.same_json(previous['agent_vaults'], live.VAULT_CAPABILITY)
                and package.same_json(target['agent_vaults'], live.VAULT_CAPABILITY)
                and package.same_json(previous.get('agent_sessions'), live.VAULT_SESSION)
                and package.same_json(target.get('agent_sessions'), live.OAUTH_SESSION)):
            previous = dict(previous, agent_sessions=live.OAUTH_SESSION)
    if 'agent_vaults' not in previous and 'agent_vaults' in target:
        from tools.serve_recovery import profile_live_reuse as live
        if (package.same_json(previous.get('agent_sessions'), live.SESSION_CAPABILITY)
                and package.same_json(target.get('agent_sessions'), live.VAULT_SESSION)
                and package.same_json(target['agent_vaults'], live.VAULT_CAPABILITY)):
            previous = dict(previous, agent_sessions=dict(live.SESSION_CAPABILITY,
                            tool_types=live.VAULT_SESSION['tool_types']))
    old, new = previous.get('realtime', {}), target.get('realtime', {})
    if ('live' not in previous and 'live' in target and
            type(old) is dict and type(new) is dict and
            old.get('pending_execution') == ['conversation', 'live'] and
            new.get('pending_execution') == []):
        # semantic_capabilities verified the actual Live/calls/secret declarations.
        # Preserve both raw observations for permit hashes and compare every
        # unrelated field with the usual additive-only rule.
        previous = dict(previous, realtime=dict(old, pending_execution=[]))
    return extends(previous, target)


def observations(registry, evidence):
    profile_contract.validate(registry)
    ledger.keys(evidence, profile_contract.NAMES)
    for name, item in evidence.items():
        ledger.keys(item, ('record', 'sha256'))
        profile_evidence.validate(item['record'], registry, name, item['sha256'])
    result = profile_contract.capabilities(evidence['primary']['record']['capabilities'], True)
    # The protocol fingerprint identifies code, not a feature that may be
    # silently lost on recovery. Each original fingerprint remains bound to
    # its own actual engine/qualification and independent admission checks.
    del result['protocol_source_sha256']
    return result


def permit(previous, target, previous_evidence, target_evidence,
           previous_config, target_config, *, automatic=False,
           allow_capacity_change=False, allow_capability_extension=False,
           reviewed_content=None):
    first = observations(previous, previous_evidence)
    second = observations(target, target_evidence)
    old_contract, new_contract = map(semantic_capabilities, (first, second))
    extension = old_contract != new_contract
    ledger.require(type(allow_capability_extension) is bool and
        (not extension or (allow_capability_extension and not automatic and
                           explicit_extension(old_contract, new_contract))),
        'migration would change application capabilities without an explicit '
        'qualified extension; automatic recovery cannot lose or add features')
    if automatic:
        for name in profile_contract.NAMES:
            prior = previous_evidence[name]['record']['business'].get('live', {})
            target_live = target_evidence[name]['record']['business'].get('live', {})
            ledger.require('interruption.json' not in prior.get('evidence_sha256', {}) or
                           'interruption.json' in target_live.get('evidence_sha256', {}),
                           'automatic fallback would lose qualified agent interruption recovery')
    old_base, new_base = (registry['identity'].get('native_tts_base')
                          for registry in (previous, target))
    base_capacity = (tts_capacity_change(old_base, new_base, base=True)
        if old_base is not None and new_base is not None else False)
    ledger.require(old_base == new_base or base_capacity or
        (old_base is None and new_base is not None and extension and
         allow_capability_extension and not automatic),
        'migration would change or remove qualified TTS Base identity/capacity')
    old_asr, new_asr = (registry['identity'].get('native_asr')
                        for registry in (previous, target))
    audio_extension, asr_capacity = (audio_change(old_asr, new_asr,
        before_voice=old_base is not None, after_voice=new_base is not None)
        if old_asr is not None and new_asr is not None else (False, False))
    old_tts, new_tts = (r['identity'].get('native_tts') for r in (previous, target))
    tts_capacity = (tts_capacity_change(old_tts, new_tts)
        if old_tts is not None and new_tts is not None else False)
    ledger.require(old_tts == new_tts or tts_capacity or
        (old_tts is None and new_tts is not None and extension and
         allow_capability_extension and not automatic),
        'migration would change or remove qualified TTS identity/capacity')
    changed = previous['profiles'] != target['profiles']
    ledger.require(type(allow_capacity_change) is bool and
        (not (changed or asr_capacity or tts_capacity or base_capacity) or
         (allow_capacity_change and not automatic)),
        'capacity change requires explicit historical-qualification migration')
    before_assets = previous['identity']['model_assets']
    after_assets = target['identity']['model_assets']
    if before_assets != after_assets:
        # This binding is supplied only after exact accepted-origin, audit,
        # upstream index, changed file and management adapter verification.
        before = ledger.digest(before_assets)
        after = ledger.digest(after_assets)
        ledger.require(reviewed_content is not None and (
            (before == reviewed_content['observed_inventory'] and
             after == reviewed_content['sealed_inventory']) or
            (not automatic and before == reviewed_content['sealed_inventory'] and
             after == reviewed_content['observed_inventory'])),
            'migration would change fixed model identity')
    for key in ('model_files', 'native_embedding'):
        ledger.require(previous['identity'][key] == target['identity'][key],
                       'migration would change fixed model identity')
    ledger.require(old_asr == new_asr or (asr_capacity and not audio_extension) or
        (audio_extension and extension and allow_capability_extension and not automatic) or
        (old_asr is None and new_asr is not None and extension and
         allow_capability_extension and not automatic),
        'migration would change or remove qualified ASR identity/capacity')
    policy.configuration(previous_config); policy.configuration(target_config)
    ledger.require(type(automatic) is bool and
        (not automatic or previous_config == target_config),
        'automatic fallback cannot alter profile policy')
    voice = (old_base is not None or new_base is not None) and (
        extension or changed or asr_capacity or tts_capacity or base_capacity)
    return validate(dict(format=VOICE_FORMAT if voice else
        TTS_CAPACITY_FORMAT if tts_capacity else
        AUDIO_EXTENSION_FORMAT if audio_extension else
        BACKEND_CAPACITY_FORMAT if asr_capacity else
        EXTENSION_FORMAT if extension else
        CAPACITY_FORMAT if changed else FORMAT,
        from_registry=profile_contract.digest(previous),
        to_registry=profile_contract.digest(target),
        profiles={n:ledger.digest(v) for n,v in previous['profiles'].items()},
        from_configuration=copy.deepcopy(previous_config),
        to_configuration=copy.deepcopy(target_config),
        from_identity=ledger.digest(previous['identity']),
        to_identity=ledger.digest(target['identity']),
        capabilities_sha256=ledger.digest(first), automatic=automatic,
        **(dict(target_profiles={n:ledger.digest(v) for n,v in target['profiles'].items()})
           if voice or changed or extension or asr_capacity or audio_extension or tts_capacity else {}),
        **(dict(target_capabilities_sha256=ledger.digest(second))
           if voice or extension or asr_capacity or audio_extension or tts_capacity else {}),
        **(dict(from_native_asr=copy.deepcopy(old_asr),
                to_native_asr=copy.deepcopy(new_asr))
           if voice or asr_capacity or audio_extension or tts_capacity else {}),
        **(dict(from_native_tts=copy.deepcopy(old_tts),
                to_native_tts=copy.deepcopy(new_tts)) if voice or tts_capacity else {}),
        **(dict(from_native_tts_base=copy.deepcopy(old_base),
                to_native_tts_base=copy.deepcopy(new_base)) if voice else {})))


def voice_transition(value):
    """Validate family/context changes without losing historical qualification."""
    added = changed = False
    contexts = []
    for prefix in ('from', 'to'):
        builtin, base = (value[prefix+'_native_'+name]
                         for name in ('tts', 'tts_base'))
        for backend, is_base in ((builtin, False), (base, True)):
            if backend is not None:
                profile_contract.tts_configuration(backend, base=is_base)
        ledger.require(builtin is None or base is None or
            builtin['limits'] == base['limits'],
            'voice migration families must share effective limits')
        contexts.append(base is not None)
    ledger.require(any(contexts), 'voice migration lacks a Base binding')
    for name in ('tts', 'tts_base'):
        before, after = (value[prefix+'_native_'+name] for prefix in ('from', 'to'))
        if before is None:
            added |= after is not None
        else:
            ledger.require(after is not None, 'voice migration removes an accepted TTS family')
            changed |= tts_capacity_change(before, after, base=name == 'tts_base')
    before, after = (value[prefix+'_native_asr'] for prefix in ('from', 'to'))
    if before is None:
        if after is not None:
            profile_contract.asr_configuration(after, voice_registration=contexts[1])
            added = True
    else:
        ledger.require(after is not None, 'voice migration removes accepted ASR')
        audio_added, audio_changed = audio_change(before, after,
            before_voice=contexts[0], after_voice=contexts[1])
        added |= audio_added
        changed |= audio_changed
    feature_change = value['target_capabilities_sha256'] != value['capabilities_sha256']
    ledger.require(not added or feature_change,
                   'voice backend addition lacks changed capabilities')
    ledger.require(changed or feature_change or value['profiles'] != value['target_profiles'],
                   'voice migration lacks an actual capacity or feature change')


def validate(value):
    capacity_change = type(value) is dict and value.get('format') == CAPACITY_FORMAT
    extension = type(value) is dict and value.get('format') == EXTENSION_FORMAT
    backend = type(value) is dict and value.get('format') == BACKEND_CAPACITY_FORMAT
    audio = type(value) is dict and value.get('format') == AUDIO_EXTENSION_FORMAT
    tts = type(value) is dict and value.get('format') == TTS_CAPACITY_FORMAT
    voice = type(value) is dict and value.get('format') == VOICE_FORMAT
    ledger.keys(value, ('format','from_registry','to_registry','profiles',
        'from_configuration','to_configuration','from_identity','to_identity',
        'capabilities_sha256','automatic',
        *(('target_profiles',) if capacity_change or extension or backend or audio or tts or voice else ()),
        *(('target_capabilities_sha256',) if extension or backend or audio or tts or voice else ()),
        *(('from_native_asr', 'to_native_asr') if backend or audio or tts or voice else ()),
        *(('from_native_tts', 'to_native_tts') if tts or voice else ()),
        *(('from_native_tts_base', 'to_native_tts_base') if voice else ())))
    ledger.require(value['format'] in (FORMAT, CAPACITY_FORMAT, EXTENSION_FORMAT,
                                      BACKEND_CAPACITY_FORMAT, AUDIO_EXTENSION_FORMAT,
                                      TTS_CAPACITY_FORMAT, VOICE_FORMAT) and
                   type(value['automatic']) is bool,
                   'invalid profile migration permit')
    if capacity_change or extension or backend or audio or tts or voice:
        ledger.keys(value['target_profiles'], profile_contract.NAMES)
        ledger.require(not value['automatic'] and
            (extension or backend or audio or tts or voice or value['target_profiles'] != value['profiles']),
            'capacity/feature migration must be explicit')
    if extension or audio:
        ledger.require(value['target_capabilities_sha256'] != value['capabilities_sha256'],
                       'extension must bind different actual capabilities')
    if backend or audio:
        before, after = value['from_native_asr'], value['to_native_asr']
        added, changed = audio_change(before, after)
        ledger.require(added if audio else changed and not added,
                       'backend migration must bind its real addition or capacity change')
    if tts:
        ledger.require(tts_capacity_change(value['from_native_tts'], value['to_native_tts']),
                       'TTS capacity migration must bind a real budget change')
        before, after = value['from_native_asr'], value['to_native_asr']
        if before != after:
            if before is None:
                profile_contract.asr_configuration(after)
                added = True
            else:
                added, _ = audio_change(before, after)
            ledger.require(not added or value['target_capabilities_sha256'] !=
                value['capabilities_sha256'], 'audio extension lacks changed capabilities')
    if voice:
        voice_transition(value)
    ledger.keys(value['profiles'], profile_contract.NAMES)
    for h in [value[k] for k in ('from_registry','to_registry','from_identity',
                               'to_identity','capabilities_sha256')]+list(value['profiles'].values())+list(
                                   value.get('target_profiles', {}).values())+(
                                   [value['target_capabilities_sha256']]
                                   if extension or backend or audio or tts or voice else []):
        ledger.require(type(h) is str and ledger.HASH.fullmatch(h),
                       'invalid profile migration identity')
    policy.configuration(value['from_configuration'])
    policy.configuration(value['to_configuration'])
    ledger.require(not value['automatic'] or
        value['from_configuration'] == value['to_configuration'],
        'automatic profile policy change')
    return value


def candidate(previous, authorization, stamp):
    validate(authorization); policy.validate(previous)
    ledger.require(previous['registry_sha256'] == authorization['from_registry'] and
        previous['profiles'] == authorization['profiles'] and
        previous['configuration'] == authorization['from_configuration'],
        'profile migration predecessor differs')
    # No elapsed time, including same-boot STOPPED time, earns new budget.
    value = policy.resume(previous, stamp)
    if authorization['format'] in (BACKEND_CAPACITY_FORMAT, AUDIO_EXTENSION_FORMAT,
                                    TTS_CAPACITY_FORMAT, VOICE_FORMAT) or (
            authorization['format'] in (CAPACITY_FORMAT, EXTENSION_FORMAT) and
            authorization['target_profiles'] != previous['profiles']):
        value = policy.historical(value)
        value['profiles'] = copy.deepcopy(authorization['target_profiles'])
    value.update(registry_sha256=authorization['to_registry'],
                 configuration=copy.deepcopy(authorization['to_configuration']))
    if value['history']:
        value['not_before_ns'] = max(value['not_before_ns'],
            value['history'][-1]['at_logical_ns']+
            value['configuration']['cooldown_seconds']*ledger.SECOND)
    if value['light_since_ns'] is not None:
        # Residency was also an established wait, even though v1 represented
        # it as light_since + configuration rather than a stored deadline.
        value['not_before_ns'] = max(value['not_before_ns'],
            value['light_since_ns']+max(
                previous['configuration']['minimum_light_seconds'],
                value['configuration']['minimum_light_seconds'])*ledger.SECOND)
    return policy.validate(value)


class AuthorizedContract:
    """Authority supplied by locked deployment or the private native root.

    On a crash the durable deployment intent must be reverified to reconstruct
    this contract. No migration token is copied into an ordinary profile worker request
    or the backward-readable profile state. Original receipts remain intact.
    """
    MAX_BYTES = policy.MAX_BYTES
    validate = staticmethod(policy.validate)
    revision = staticmethod(policy.revision)
    digest = staticmethod(policy.digest)

    def __init__(self, authorization):
        self.authorization = copy.deepcopy(validate(authorization))

    def transition(self, previous, target):
        try:
            return policy.transition(previous, target)
        except (ValueError, KeyError, TypeError):
            stamp = ledger.Stamp(target['clock']['boot_id'], target['clock']['monotonic_ns'])
            expected = candidate(previous, self.authorization, stamp)
            ledger.require(target == expected, 'migration changed protected profile history')
            return target

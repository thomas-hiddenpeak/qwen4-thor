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


def asr_capabilities(value):
    """Optional activation adds a qualified backend, never removes one."""
    selected = value.get('transcriptions')
    if selected is None:
        return
    ledger.require(type(selected) is dict and
        all(selected.get(k) == v for k, v in package.NATIVE_ASR.items()) and
        type(selected.get('configured')) is bool,
        'invalid native ASR capability identity')
    selected['configured'] = dict(enabled=True) if selected['configured'] else {}


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
    asr_capabilities(result)
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
           allow_capacity_change=False, allow_capability_extension=False):
    first = observations(previous, previous_evidence)
    second = observations(target, target_evidence)
    old_contract, new_contract = map(semantic_capabilities, (first, second))
    extension = old_contract != new_contract
    ledger.require(type(allow_capability_extension) is bool and
        (not extension or (allow_capability_extension and not automatic and
                           extends(old_contract, new_contract))),
        'migration would change application capabilities without an explicit '
        'qualified extension; automatic recovery cannot lose or add features')
    old_asr, new_asr = (registry['identity'].get('native_asr')
                        for registry in (previous, target))
    asr_capacity = old_asr is not None and new_asr is not None and \
        old_asr['limits'] != new_asr['limits']
    changed = previous['profiles'] != target['profiles']
    ledger.require(type(allow_capacity_change) is bool and
        (not (changed or asr_capacity) or (allow_capacity_change and not automatic)),
        'capacity change requires explicit historical-qualification migration')
    for key in ('model_files', 'model_assets', 'native_embedding'):
        ledger.require(previous['identity'][key] == target['identity'][key],
                       'migration would change fixed model identity')
    ledger.require(old_asr == new_asr or (asr_capacity and
        all(old_asr[k] == new_asr[k] for k in package.NATIVE_ASR)) or
        (old_asr is None and new_asr is not None and extension and
         allow_capability_extension and not automatic),
        'migration would change or remove qualified ASR identity/capacity')
    policy.configuration(previous_config); policy.configuration(target_config)
    ledger.require(type(automatic) is bool and
        (not automatic or previous_config == target_config),
        'automatic fallback cannot alter profile policy')
    return validate(dict(format=BACKEND_CAPACITY_FORMAT if asr_capacity else
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
           if changed or extension or asr_capacity else {}),
        **(dict(target_capabilities_sha256=ledger.digest(second))
           if extension or asr_capacity else {}),
        **(dict(from_native_asr=copy.deepcopy(old_asr),
                to_native_asr=copy.deepcopy(new_asr)) if asr_capacity else {})))


def validate(value):
    capacity_change = type(value) is dict and value.get('format') == CAPACITY_FORMAT
    extension = type(value) is dict and value.get('format') == EXTENSION_FORMAT
    backend = type(value) is dict and value.get('format') == BACKEND_CAPACITY_FORMAT
    ledger.keys(value, ('format','from_registry','to_registry','profiles',
        'from_configuration','to_configuration','from_identity','to_identity',
        'capabilities_sha256','automatic',
        *(('target_profiles',) if capacity_change or extension or backend else ()),
        *(('target_capabilities_sha256',) if extension or backend else ()),
        *(('from_native_asr', 'to_native_asr') if backend else ())))
    ledger.require(value['format'] in (FORMAT, CAPACITY_FORMAT, EXTENSION_FORMAT,
                                      BACKEND_CAPACITY_FORMAT) and
                   type(value['automatic']) is bool,
                   'invalid profile migration permit')
    if capacity_change or extension or backend:
        ledger.keys(value['target_profiles'], profile_contract.NAMES)
        ledger.require(not value['automatic'] and
            (extension or backend or value['target_profiles'] != value['profiles']),
            'capacity/feature migration must be explicit')
    if extension:
        ledger.require(value['target_capabilities_sha256'] != value['capabilities_sha256'],
                       'extension must bind different actual capabilities')
    if backend:
        before, after = value['from_native_asr'], value['to_native_asr']
        for native in (before, after):
            ledger.keys(native, (*package.NATIVE_ASR, 'limits'))
            package.asr_settings(dict(native, directory='/qualified/asr-assets'))
        ledger.require(all(before[k] == after[k] for k in package.NATIVE_ASR) and
            before['limits'] != after['limits'],
            'backend migration must bind the same model and changed ASR limits')
    ledger.keys(value['profiles'], profile_contract.NAMES)
    for h in [value[k] for k in ('from_registry','to_registry','from_identity',
                               'to_identity','capabilities_sha256')]+list(value['profiles'].values())+list(
                                   value.get('target_profiles', {}).values())+(
                                   [value['target_capabilities_sha256']]
                                   if extension or backend else []):
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
    if authorization['format'] == BACKEND_CAPACITY_FORMAT or (
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

"""Keep the accepted tracing-era record recoverable alongside real Live business."""
import copy
import hashlib

from tools.serve_recovery import ledger, profile_mcp_codec as codec

FORMAT = 'q4t-live-profile-source-reuse-v1'
RECORD_FORMAT = 'q4t-profile-business-v23'
BETA_FORMAT = 'q4t-live-beta-profile-source-reuse-v1'
BETA_RECORD_FORMAT = 'q4t-profile-business-v24'
AGENT_RECORD_FORMAT = 'q4t-profile-business-v25'
AGENT_FORMAT = 'q4t-agent-profile-source-reuse-v1'
AGENT_REFRESH = 'q4t-agent-profile-requalification-v1'
REGISTRATION_RECORD = 'q4t-profile-business-v26'
REGISTRATION_FORMAT = 'q4t-agent-registration-profile-source-v1'
SESSION_RECORD = 'q4t-profile-business-v27'
SESSION_FORMAT = 'q4t-agent-session-profile-source-v1'
VAULT_RECORD = 'q4t-profile-business-v28'
VAULT_FORMAT = 'q4t-agent-vault-profile-source-v1'
WORKSPACE_RECORD = 'q4t-profile-business-v31'
WORKSPACE_FORMAT = 'q4t-agent-workspace-source-v1'
ARTIFACT_RECORD = 'q4t-profile-business-v32'
ARTIFACT_FORMAT = 'q4t-session-artifact-source-v1'
MANAGED_TOOL_RECORD = 'q4t-profile-business-v33'
MANAGED_TOOL_FORMAT = 'q4t-managed-session-tools-source-v1'
HOSTED_LIFECYCLE_RECORD = 'q4t-profile-business-v42'
HOSTED_LIFECYCLE_FORMAT = 'q4t-hosted-environment-lifecycle-source-v1'
HOSTED_LIFECYCLE_CAPABILITY = dict(prewarm_seconds=300, max_sessions=128,
    idle_seconds=1200, checkpoint_seconds=604800, live_event_retention=64,
    shared_files=True, turn_scoped_resume=True, pending_tool_keeps_active=True)
HOSTED_LIFECYCLE_REVIEWED = {
    'primary': '68481ee9e52c56bc0bd993214a52da4d1ebe654a28ab79ada05d7e30a5fbb1b0',
    'light': '99a16787d773574d10009467ccec6aaeeed421e365fbf22bde69a77d109e8ee7',
}
WEBHOOK_RECORD = 'q4t-profile-business-v41'
WEBHOOK_FORMAT = 'q4t-webhook-source-v1'
WEBHOOK_CAPABILITY = dict(policy_profile='q4t-webhook-delivery-policy-v1',
    endpoint='/v1/webhook_endpoints', event_types='/v1/webhook_event_types',
    delivery='at_least_once', max_retry_seconds=259200, https_only=True,
    request_override=False, core_readiness_dependency=False)
WEBHOOK_REVIEWED = {
    'primary': 'fb46c5e00a4d9a2d6cfa59e781ebe08fd8d684d8a51c3b9cb12474c5592726e9',
    'light': 'af923ee17d2cef62d6eefee8033a69b4d3fa4b6fb6400b49568c9cd6fa0b1fb1',
}
AUTOMATION_REVIEWED = {
    'primary': '39378d4ab67df2449d4f0953faf466c3be2d2a413b74c8abe57b79417aafa1d4',
    'light': 'ded04edfd79a1cd60454ae157a47c0f62ed02ddc81b85e563cfe8bc8b613cede',
}
AUTOMATION_RECORD = 'q4t-profile-business-v40'
AUTOMATION_FORMAT = 'q4t-agent-automation-source-v1'
AUTOMATION_CAPABILITY = dict(selector='agents=v1', endpoint='/v1/agents/automations',
    schedules=['once','periodic'], targets=['new_session','existing_session'],
    manual_invocations=True, session_tool='manage_automation', session_scope='creator_only',
    tool_receipt='atomic_with_mutation')
TRANSLATOR_RECORD = 'q4t-profile-business-v39'
TRANSLATOR_FORMAT = 'q4t-translation-transport-source-v1'
TRANSLATOR_CAPABILITY = dict(client_secrets='/v1/realtime/translations/client_secrets',
    websocket='/v1/realtime/translations', calls='/v1/realtime/translations/calls',
    webrtc_output='remote_audio_track', data_channel='optional',
    requires=['native_asr','text_model','native_tts'])
TRANSLATOR_REVIEWED = {
    'primary': '2845a42aa6265ef5dc85a6c7bff236e6147067166bee588623a46795ed340a3e',
    'light': 'ad144a7a4324c52b3caea26fb7270ab8a4c831999215cfa192602303506ce478',
}
CODE_RECORD = 'q4t-profile-business-v38'
CODE_FORMAT = 'q4t-interpreter-workspace-source-v1'
CODE_CAPABILITY = dict(runtime_requirement='system_cpython_3_12', workspace='/mnt/data',
                       skill_mount='/mnt/skills', session_state='files', responses_citations=True,
                       assistant_output_files=True, file_limit=256, file_bytes=8388608,
                       workspace_bytes=16777216, network='disabled')
CODE_REVIEWED = {
    'primary': '9f9b7800cfc6e92efe0ce6009767c4dc4d6237cafdc526c31bf2db8fec4c2a44',
    'light': 'a841cfa9cacd791bd8425eebb5652c87a72ce7328d44f079c9a547d5f17a55f0',
}
SKILLS_RECORD = 'q4t-profile-business-v37'
SKILLS_FORMAT = 'q4t-skill-workspace-source-v1'
SKILLS_CAPABILITY = dict(endpoint='/v1/containers', skills='/v1/skills', skill_versions=True,
                         skill_mount='immutable_snapshot', file_contents=True)
SKILLS_REVIEWED = {
    'primary': 'dab417dc1d87af8a07f9636a2748e5ae5da0528ffdaa9ed02426c6d8a5c1874d',
    'light': '00985afbf62b8a7d2521003e6830763ff35073ef53e1fef862e27c84c8501e90',
}
ASSISTANTS_RECORD = 'q4t-profile-business-v36'
ASSISTANTS_FORMAT = 'q4t-assistants-run-source-v1'
ASSISTANTS_CAPABILITY = dict(selector='assistants=v2', endpoint='/v1/assistants', threads='/v1/threads',
    messages='/v1/threads/{thread_id}/messages', runs='/v1/threads/{thread_id}/runs',
    run_steps=True, submit_tool_outputs=True)
ASSISTANTS_REVIEWED = {
    'primary': '2016b070e7fb19a74b4bd421d27141642de84336dd1f0b6f09de948677628881',
    'light': 'a43a354cc03ef066bbd100712284a6dccdfe3b332b4f02b3dd9a879e41d8f58d',
}
TRANSLATION_RECORD = 'q4t-profile-business-v35'
TRANSLATION_FORMAT = 'q4t-translation-realtime-source-v1'
TRANSLATION_CAPABILITY = dict(endpoint='/v1/audio/translations', models=['whisper-1', 'qwen3-asr-0.6b'],
    backend='native_asr_and_chat', language='english', configured=True,
    formats=['json','text','verbose_json','srt','vtt'], subtitles_require='aligner-model-dir')
TRANSLATION_REVIEWED = {
    'primary': '75d0e41fe4033bca234cffebed3163869a3e2b5cebf1315ba639c0c36461ac96',
    'light': 'b7951dcde8dcbb1b3d75886640f23c218a5f77003d534c16fbd1b5e7b81d5b69',
}
SUBAGENT_RECORD = 'q4t-profile-business-v34'
SUBAGENT_FORMAT = 'q4t-managed-subagent-source-v1'
SUBAGENT_REVIEWED = {
    'primary': {
        'bc31006134f64f662919accea942c0d675d9ef9e5ad4fee50879eb3c1f66a292',
        'aefe830b6cafa11377e50e22e9fe4775bf42df2b9c9a6a459b24dd62befd0a73',
    },
    'light': {
        '6929d4f5200e4e59e3c4402cc531029c069214c5f581ab318affde4804b67822',
        'c325c73cc2584ec49f03a24e5a9ae09e2162ec49d9c84726cd70fab9faf5cd6c',
    },
}
SUBAGENT_CAPABILITY = dict(selector='agents=v1', endpoint='/v1/agents/sessions/{session_id}/subagents',
                           reads=['list','retrieve','items','turns','turn','turn_items'],
                           lifecycle=['create','interrupt','close','resume'], turn_events=True, session_memory=True)
MANAGED_TOOL_REVIEWED = {
    'primary': 'a861225ac8bf4e9216fc7670b152039340237ab83e8f002854c8f92d2cb58cc2',
    'light': '190809a136db0c5f0dec62fc808cd39df9db137470cf374da18640fe9801a13b',
}
ARTIFACT_CAPABILITY = dict(selector='agents=v1', source='/workspace/outputs/', immutable=True,
                           operations=['list', 'retrieve', 'content', 'delete'])
ARTIFACT_REVIEWED = {
    'primary': '9134b3c35009291ee215d9d807508c0171001eddc6aa8e01ec2f3111a0fb1f32',
    'light': '8ef5c5c0378b0c1ffafc830755607e43fbad9679e98bf6a11b66f5cfc09cb0d0',
}
WORKSPACE_CAPABILITY = dict(selector='agents=v1', prewarm_seconds=300,
                            idempotency_seconds=86400, workspace_files=True,
                            network=['disabled'], between_turn_writes=True)
WORKSPACE_REVIEWED = {
    'primary': {
        '405210114da0add45510e800d673dff2176f62fd8123e78a0a74ac80c33f65ac',
        '6e28e44b71aab96b044d30301e8b9aa2a36fe882031e4a35d350efa10bd3f846',
        'a4641474c6326b0fd6c40faed6ff8eea95fa1c4a70277dd2061cb9f95d430faa',
    },
    'light': {
        'b2051c6ba5674639ed476bf988487509fc9860aa4561bfc76c27eb12f4629f70',
        '994c7b2df26b9c275feec6ff859995ea50d92113cee096f764ac1cbacb90b9e5',
        '9d5abc3509687c8f38d690e97c111b80dfc5e07fd0fd47be0976608624182803',
    },
}
ENVIRONMENT_RECORD = 'q4t-profile-business-v30'
ENVIRONMENT_FORMAT = 'q4t-agent-environment-template-source-v1'
TEMPLATE_CAPABILITY = dict(selector='agents=v1', operations=['create','retrieve','update','list','delete'],
                           confidential_definition='sealed', default_network='disabled')
SESSION_TRACE_CAPABILITY = dict(selector='agents=v1', endpoint='/v1/agents/sessions/{session_id}/traces',
                                format='otlp_json', anchor='root_turn_id', published_only=True)
ENVIRONMENT_REVIEWED = {
    'primary': '2dfe88998e517324a5cb71f02a3cc6d3135157caca3ae49d076755aa196a453f',
    'light': '608e130e4026322b69666b27572caee1c53d7558bf279771ee700c19f95f5125',
}
OAUTH_RECORD = 'q4t-profile-business-v29'
OAUTH_FORMAT = 'q4t-vault-oauth-profile-source-v1'
SESSION_CAPABILITY = dict(selector='agents=v1', endpoint='/v1/agents/sessions',
    environments=['none'], tool_types=['function'], stream=True,
    idempotent_input_events=True, cancel_turn=True, storage='authenticated_client')
SESSION_REVIEWED = {
    'primary': '3d6691759a569a94a7f9aaaa010f6eacc7bf10ca798a220b015139d73248e329',
    'light': '5c3e6a8a050d64e4da85319ea7bbb23aff80a80a72952c555f348599f151bafe',
}
VAULT_SESSION = dict(SESSION_CAPABILITY, tool_types=['function', 'mcp'],
    mcp=dict(connection_origin='service', transport='http',
             vault_auth=['static_bearer', 'mcp_oauth_access_token']))
OAUTH_SESSION = dict(VAULT_SESSION, mcp=dict(VAULT_SESSION['mcp'],
    vault_auth=[*VAULT_SESSION['mcp']['vault_auth'], 'mcp_oauth_refresh']))
WORKSPACE_SESSION = dict(OAUTH_SESSION, environments=['none', 'openai_hosted'])
MANAGED_TOOL_SESSION = dict(WORKSPACE_SESSION, tool_types=['function', 'mcp', 'tool_search', 'web_search'],
                            time_awareness='utc_at_each_model_step')
OAUTH_REVIEWED = {
    'primary': '1fb036ff722a21719546d1b042298cc91e489ecd9dc30ddcfb8602e546c683ef',
    'light': 'cc67e878110002a8be19fa7fbf61fe32d82b098731e570bf2008e8d0dfa54517',
}
VAULT_CAPABILITY = dict(selector='agents=v1', endpoint='/v1/vaults',
    operations=['create', 'retrieve', 'update', 'list', 'delete'],
    storage='authenticated_client', lifecycle='archive',
    credentials=dict(auth=['mcp_oauth', 'static_bearer', 'environment_variable'],
                     write_only=True, rotation=True, archive_cascade=True))
VAULT_REVIEWED = {
    'primary': '2849d28c117d197f0ceb0bc6f3a6a43d63f2a5f0173766f82eae0399bb75e939',
    'light': 'e76621fdd0353aee0007baf9d55ae55c6c256b121be9ec0194757bf7e95a9c69',
}
REGISTRATION_CAPABILITY = dict(selector='agents=v1', endpoint='/v1/agents',
    operations=['create','retrieve','update','list','delete'],
    storage='authenticated_client', retention='until_deleted', tool_list_max_bytes=3145728)
REGISTRATION_REVIEWED = {
    'primary': 'd957150126ea565ce9d23c4b8707100cae80f95fd1b4ab5c81d5021e916565e1',
    'light': '2e68838e87b3fe8fb4f0b578269dac10ccb9fc000a34b490a57562613bff0736',
}

REFRESH_REVIEWED = {
    'primary': 'a759ecb078ce7791679061f24560ec4a0c9622e47732ab7dd675c97fdf4fe49f',
    'light': '5265a9815da252b78560b391e1cb2b441ae35e9c8e2cbc89d450e6e1a0e99dd5',
}
AGENT_REVIEWED = {
    'primary': 'fa7353fa12ef7aef3514e25cb97ecff0bffa49e260267296ceb4ff6ee179cbad',
    'light': 'de3a519c4411138d02a6d863704d1c8142a5bb0334257ad35b0a99e81f692f06',
}
REUSE_FORMAT = {RECORD_FORMAT: FORMAT, BETA_RECORD_FORMAT: BETA_FORMAT,
                AGENT_RECORD_FORMAT: AGENT_FORMAT, REGISTRATION_RECORD: REGISTRATION_FORMAT,
                SESSION_RECORD: SESSION_FORMAT, VAULT_RECORD: VAULT_FORMAT,
                OAUTH_RECORD: OAUTH_FORMAT, ENVIRONMENT_RECORD: ENVIRONMENT_FORMAT,
                WORKSPACE_RECORD: WORKSPACE_FORMAT, ARTIFACT_RECORD: ARTIFACT_FORMAT,
                MANAGED_TOOL_RECORD: MANAGED_TOOL_FORMAT, SUBAGENT_RECORD: SUBAGENT_FORMAT,
                TRANSLATION_RECORD: TRANSLATION_FORMAT, ASSISTANTS_RECORD: ASSISTANTS_FORMAT,
                SKILLS_RECORD: SKILLS_FORMAT, CODE_RECORD: CODE_FORMAT,
                TRANSLATOR_RECORD: TRANSLATOR_FORMAT, AUTOMATION_RECORD: AUTOMATION_FORMAT,
                WEBHOOK_RECORD: WEBHOOK_FORMAT, HOSTED_LIFECYCLE_RECORD: HOSTED_LIFECYCLE_FORMAT}
BETA_REVIEWED = {
    'primary': 'e2178b021652317a2d67189b73982edc3a7a1b70c4c2716929a448a5d579f07a',
    'light': '19299d0067ef28685380c4bf080526c2463e5f083ae9868c98bf6c869a92cd0c',
}
METADATA = ('format', 'configuration', 'startup_memory', 'environment', 'reused_business')
# Receipts are canonical profile record bytes including their terminating newline.
REVIEWED = {
    'primary': {
        'previous': '6255bf549874162c3e846206528a706af463f4c5facfffe0d087918e349858a5',
        'selected': 'd10054a6a8dae78f324ec37f510e32761c00568f0957d16927bbea583a587a0f'
    },
    'light': {
        'previous': '5da10f635a7ea8e289de5f114017237fa51dabcb90abadba433c7d5ef0337bf6',
        'selected': '16337b914b6b7a7b4bb476b0185b7dd9340be77eae44253d0c40f52e612aef98'
    }
}


# The SDK ran through this native ingress. The acceptance hash is carried in
# both reviewed profile records. An engine capability cannot qualify a gateway.
ACCEPTED_INGRESS = {
    '3280676f47c89ff879f88386e7ff954fc91e583d873525e3571eb5c1c12aa237':
        'ac71057cdb01b81bb3f5ba5509fe1f7e16821ac7e385676977bfe7c8be7b9f46',
    '5c530a6c2de1a1af191645ac0b0c51ee1af38ceacfa36bf6f26fa21d5168abb5':
        'ac71057cdb01b81bb3f5ba5509fe1f7e16821ac7e385676977bfe7c8be7b9f46',
    '97196543bab99ad9654f62e9ff56d591580d951abfe54d9336c0b42ada918264':
        'f06eb62270ee8efa305cda7aa7ac6b60780ba790efdb1a545c6bf18861b9a06a',
    '315bf9fd8172b37e40c0ad7de0e9ae46a98db4f2c6d9914a59306032ff43aa44':
        '7b9770f3784c047bce10342b5a085c2b6ca3a231be285f7fae3580fffe60ec36',
    '5429ed3286976393ee139a6a848ed81c8981335c373789bcb0d4a96f059fc6f3':
        '8dcbff0906e72c0109a9b36febe529e92a9708d2c2b5cb8d0c934bc27f453572',
    '7c061333749eeeeb9084b3f020adbdca3703e81470a7d95ea8caf78b11003a7b':
        '8dcbff0906e72c0109a9b36febe529e92a9708d2c2b5cb8d0c934bc27f453572',
    '432ffbbea808303224305fdbc043548f7366d175dd8c8f7bd4657c9091aa43c8':
        'f4ef4776b8db25a9a303234f8497a5595ed6082e850d71ec179717c43a481e15',
    '92a0ac0c3d173820cdb36c1002ed4f46b0063fba3bb5727f655db05f7e827cfe':
        '458b028f02d936beb055a4ffe1d4c6a55bc7766082c039339d3c2b4b05bc6919',
    '82f102fbf86efb6c7a861ae3fb0243f64402988507d8ba4748251f5642d4e433':
        '9e72f5309977c0f7ae4f3e7c192b12029a8ba6a1d845da688fc6b140c66c8f87',
    '1de6946fe64e0d94905bf146bc242b483cb07e9e0a3a83d15b0ab01d94dd57dc':
        'f5ad763378beeec259b3df29aa14a2e39cf7906440a0b6fe5cafb9a61b9b9c37',
    '555452009732e11ab1859f4e4996b23c69669691396be2d943850b40cb55e4f9':
        'a201da20bee226df67d1c7c409c0431b587dfe4b648f09d71ae046d6d6ffc5dc',
    'a2e3011a39b2d9d2cf8d55dbf93e6119b98b06281d2470d5273c1e1f49bdc37c':
        'a201da20bee226df67d1c7c409c0431b587dfe4b648f09d71ae046d6d6ffc5dc',
    'a486fc871b0c8c4b3d46e85d1ad092815836bb0f6f38ad34ad8bc4225d670b70':
        '3e1b8020a0216da9db599b78b3c64ce1d454fd56019df62c6dc5ecfb567b62ed',
    'c4007692ba30e2a42930e73a56b74068fdaa460b869f02a597f86bbf19fe48df':
        '3e1b8020a0216da9db599b78b3c64ce1d454fd56019df62c6dc5ecfb567b62ed',
    '06022952c38de6d1cebc5a77ab9f254877a73c5eae3796f2259df662b911f658':
        '3e1b8020a0216da9db599b78b3c64ce1d454fd56019df62c6dc5ecfb567b62ed',
    '6665a8f7d995806d68572b8c141110538bb689d25c872979a02ddcda5bc8d367':
        '8afa45887db80173df1841d48f11c50a507b6828e861e555018648744a07acb5',
    '3d05f0cc1c99a70f16e4f96291d6e6234a680765facc386c93d59f72ce5a40be':
        '1e71f9b806763a77a7dce8923ed9585f3d678b2105acfbdf9c9fa98be6e16593',
    '26dab53af96b229e3b653e983828f7b4ef3979e4237a564c9518eeffc307dc33':
        '1e71f9b806763a77a7dce8923ed9585f3d678b2105acfbdf9c9fa98be6e16593',
    'f742f4a99411c3f60099d219e1c4959a08c06e9878d34ff9153de1b256a3105c':
        '96449e96cf5401726e47505e3a816dacd18ad2eeb8d4c09114eb54a24a8dddc8',
    '7014bde675707b1bc3a52765ebbacfe2455a6b0be61b1eee7d4a61c3cb36dd5f':
        '4ef57fba3d32d8a213471f7db499a4e097c82b120da4e2a08dd7878a10f71d3b',
    'da34734b772404e09ed8f7ac1c91f08683a712194c27c97a4c8e69f6501e8b88':
        '72e916a3571768d7b256c3c12ff8710333ae03933ebc8d713f9b3f1ff467eec3',
    '0c2bb7554043ff66e2d59113a666b5233ac7a2a52d1042f5c0f2f1d48417ba0b':
        '72e916a3571768d7b256c3c12ff8710333ae03933ebc8d713f9b3f1ff467eec3',
    '29c31bf9ef841fe729d2fc3085888153489d398e40c7508a64a726c5a431d019':
        'b7a1967ba8540dc51ae7187577052bb1273d789d85ed8ab5a2d1cf5b02f21caf',
    '1e744c78e3c8fc9600add6ac99c800ff58c69bb4a6acf1218104fc7bd85205d6':
        'b7a1967ba8540dc51ae7187577052bb1273d789d85ed8ab5a2d1cf5b02f21caf',
}


def verify_ingress(live, digest):
    expected = ACCEPTED_INGRESS.get(live['acceptance_sha256'])
    ledger.require(expected is not None and digest == expected,
                   'installed ingress differs from the qualified Live business route')


def record_hash(value):
    return hashlib.sha256(ledger.canonical(value) + b'\n').hexdigest()


def automation_session():
    return dict(MANAGED_TOOL_SESSION,
                tool_types=[*MANAGED_TOOL_SESSION['tool_types'], 'automations'])


def format_for(capabilities):
    if 'hosted_environment_lifecycle' in capabilities:
        prior = dict(capabilities)
        selected = prior.pop('hosted_environment_lifecycle')
        ledger.require(ledger.canonical(selected) == ledger.canonical(HOSTED_LIFECYCLE_CAPABILITY)
                       and format_for(prior) == WEBHOOK_RECORD,
                       'invalid hosted workspace declaration or predecessor')
        return HOSTED_LIFECYCLE_RECORD
    if 'webhooks' in capabilities:
        previous = dict(capabilities)
        declaration = previous.pop('webhooks')
        ledger.require(ledger.canonical(declaration) == ledger.canonical(WEBHOOK_CAPABILITY)
                       and format_for(previous) == AUTOMATION_RECORD,
                       'invalid webhook declaration or predecessor')
        return WEBHOOK_RECORD
    if 'agent_automations' in capabilities:
        previous = dict(capabilities)
        declaration = previous.pop('agent_automations')
        ledger.require(ledger.canonical(declaration) == ledger.canonical(AUTOMATION_CAPABILITY) and
                       ledger.canonical(previous.get('agent_sessions')) == ledger.canonical(automation_session()) and
                       format_for(dict(previous, agent_sessions=MANAGED_TOOL_SESSION)) == TRANSLATOR_RECORD,
                       'invalid automation or session declaration or predecessor')
        return AUTOMATION_RECORD
    if 'translations' in capabilities.get('realtime', {}):
        transport = dict(capabilities['realtime'])
        declaration = transport.pop('translations')
        ledger.require(ledger.canonical(declaration) == ledger.canonical(TRANSLATOR_CAPABILITY)
                       and format_for(dict(capabilities, realtime=transport)) == CODE_RECORD,
                       'translation transports or predecessor differ')
        return TRANSLATOR_RECORD
    if 'hosted_python' in capabilities:
        previous = dict(capabilities)
        declaration = previous.pop('hosted_python')
        ledger.require(ledger.canonical(declaration) == ledger.canonical(CODE_CAPABILITY)
                       and format_for(previous) == SKILLS_RECORD,
                       'interpreter declaration or workspace predecessor differs')
        return CODE_RECORD
    if 'container_workspaces' in capabilities:
        previous = dict(capabilities)
        declaration = previous.pop('container_workspaces')
        ledger.require(ledger.canonical(declaration) == ledger.canonical(SKILLS_CAPABILITY)
                       and format_for(previous) == ASSISTANTS_RECORD,
                       'workspace declaration or Assistants predecessor differs')
        return SKILLS_RECORD
    if 'assistants' in capabilities:
        previous = dict(capabilities)
        declaration = previous.pop('assistants')
        ledger.require(ledger.canonical(declaration) == ledger.canonical(ASSISTANTS_CAPABILITY)
                       and format_for(previous) == TRANSLATION_RECORD,
                       'Assistants qualification requires native Runs and the released predecessor')
        return ASSISTANTS_RECORD
    if 'translations' in capabilities:
        previous = dict(capabilities)
        declaration = previous.pop('translations')
        ledger.require(ledger.canonical(declaration) == ledger.canonical(TRANSLATION_CAPABILITY)
                       and format_for(previous) == SUBAGENT_RECORD,
                       'translation qualification requires both native models and its predecessor')
        return TRANSLATION_RECORD
    if 'agent_session_subagents' in capabilities:
        previous = dict(capabilities)
        child = previous.pop('agent_session_subagents')
        ledger.require(ledger.canonical(child) == ledger.canonical(SUBAGENT_CAPABILITY)
                       and format_for(previous) == MANAGED_TOOL_RECORD,
                       'incomplete managed subagent or predecessor declaration')
        return SUBAGENT_RECORD
    sessions = capabilities.get('agent_sessions')
    if isinstance(sessions, dict) and 'time_awareness' in sessions:
        ledger.require(ledger.canonical(sessions) == ledger.canonical(MANAGED_TOOL_SESSION) and
                       format_for(dict(capabilities, agent_sessions=WORKSPACE_SESSION)) == ARTIFACT_RECORD,
                       'incomplete managed tool or predecessor declaration')
        return MANAGED_TOOL_RECORD
    if 'agent_session_artifacts' in capabilities:
        previous = dict(capabilities)
        artifacts = previous.pop('agent_session_artifacts')
        ledger.require(ledger.canonical(artifacts) == ledger.canonical(ARTIFACT_CAPABILITY) and
                       format_for(previous) == WORKSPACE_RECORD,
                       'artifact qualification requires immutable files and hosted workspaces')
        return ARTIFACT_RECORD
    if 'agent_environments' in capabilities:
        previous = dict(capabilities)
        workspace = previous.pop('agent_environments')
        ledger.require(ledger.canonical(workspace) == ledger.canonical(WORKSPACE_CAPABILITY) and
                       ledger.canonical(previous.get('agent_sessions')) == ledger.canonical(WORKSPACE_SESSION),
                       'invalid hosted workspace capabilities')
        previous['agent_sessions'] = OAUTH_SESSION
        ledger.require(format_for(previous) == ENVIRONMENT_RECORD,
                       'workspace requires published templates and traces')
        return WORKSPACE_RECORD
    if 'beta_query_selector' in capabilities.get('responses', {}):
        ledger.require(capabilities['responses']['beta_query_selector'] == 'beta=true',
                       'unknown qualified Responses Beta selector')
        if 'multi_agent' in capabilities['responses']:
            ledger.require(capabilities['responses']['multi_agent'] == dict(
                selector='responses_multi_agent=v1', default_max_concurrent_subagents=3,
                shared_output_budget=True, agent_events=True, actions=['spawn_agent',
                'interrupt_agent', 'list_agents', 'send_message', 'followup_task', 'wait_agent']),
                'unknown qualified hosted Responses agent capabilities')
            if 'agent_registration' in capabilities:
                ledger.require(capabilities['agent_registration'] == REGISTRATION_CAPABILITY,
                               'unknown registered Agent configuration capability')
                if 'agent_sessions' in capabilities:
                    if 'agent_vaults' in capabilities:
                        if (ledger.canonical(capabilities['agent_vaults']) == ledger.canonical(VAULT_CAPABILITY)
                                and ledger.canonical(capabilities['agent_sessions']) == ledger.canonical(OAUTH_SESSION)):
                            if 'agent_environment_templates' in capabilities or 'agent_session_traces' in capabilities:
                                ledger.require(ledger.canonical(capabilities.get('agent_environment_templates')) ==
                                               ledger.canonical(TEMPLATE_CAPABILITY) and
                                               ledger.canonical(capabilities.get('agent_session_traces')) ==
                                               ledger.canonical(SESSION_TRACE_CAPABILITY),
                                               'incomplete environment or session trace qualification')
                                return ENVIRONMENT_RECORD
                            return OAUTH_RECORD
                        ledger.require(ledger.canonical(capabilities['agent_vaults']) ==
                                       ledger.canonical(VAULT_CAPABILITY) and
                                       ledger.canonical(capabilities['agent_sessions']) ==
                                       ledger.canonical(VAULT_SESSION),
                                       'unknown Agent Vault or MCP session capabilities')
                        return VAULT_RECORD
                    ledger.require(capabilities['agent_sessions'] == SESSION_CAPABILITY,
                                   'unknown qualified Agent session capabilities')
                    return SESSION_RECORD
                return REGISTRATION_RECORD
            return AGENT_RECORD_FORMAT
        return BETA_RECORD_FORMAT
    return RECORD_FORMAT


def attach(value, prior):
    """Store only the fields needed to recover the exact accepted old record."""
    old = prior['identity']
    new = value['identity']
    previous = {k: prior[k] for k in METADATA}
    previous['engine'] = {k: old[k] for k in ('binary_sha256', 'protocol_sha256')}
    previous['ctimes'] = {n: row['ctime_ns'] for n, row in old['model_assets'].items()
                          if row['ctime_ns'] != new['model_assets'][n]['ctime_ns']}
    before, after = prior['capabilities'], value['capabilities']
    previous['capabilities'] = {k: v for k, v in before.items()
                                if k not in after or ledger.canonical(v) != ledger.canonical(after[k])}
    previous['added'] = sorted(after.keys() - before.keys())
    agent = value['format'] == AGENT_RECORD_FORMAT
    refresh = agent and prior['format'] == AGENT_RECORD_FORMAT
    registration = value['format'] == REGISTRATION_RECORD
    session = value['format'] == SESSION_RECORD
    vault = value['format'] == VAULT_RECORD
    oauth = value['format'] == OAUTH_RECORD
    templates = value['format'] == ENVIRONMENT_RECORD
    workspace = value['format'] == WORKSPACE_RECORD
    revision = value['format'] in REUSE_FORMAT and value['format'] != RECORD_FORMAT
    if value['format'] in (HOSTED_LIFECYCLE_RECORD, WEBHOOK_RECORD, AUTOMATION_RECORD):
        predecessors = {
            HOSTED_LIFECYCLE_RECORD: (WEBHOOK_RECORD, WEBHOOK_REVIEWED, 'hosted_environment_lifecycle',
                                      'environment-handoff.json'),
            WEBHOOK_RECORD: (AUTOMATION_RECORD, AUTOMATION_REVIEWED, 'webhooks', 'webhook-handoff.json'),
            AUTOMATION_RECORD: (TRANSLATOR_RECORD, TRANSLATOR_REVIEWED, 'agent_automations', 'automation-handoff.json'),
        }
        previous_format, reviewed, added, receipt = predecessors[value['format']]
        changed = {'protocol_source_sha256'}
        if value['format'] == AUTOMATION_RECORD:
            changed.add('agent_sessions')
        proofs = value['business']['live']['evidence_sha256']
        old = prior['business']['live']['evidence_sha256']
        ledger.require(prior['format'] == previous_format and
                       record_hash(prior) == reviewed.get(prior['profile']) and
                       previous['added'] == [added] and
                       set(previous['capabilities']) == changed and
                       format_for(after) == value['format'] and
                       old.keys() | {receipt} == proofs.keys() and
                       all(proofs[k] == v for k,v in old.items() if k not in
                           ('configuration.wire','capabilities.json','core-after.wire',
                            'observation.json','result.json','cleanup.json','plan.json')) and
                       ledger.HASH.fullmatch(proofs[receipt]),
                       'qualification needs the issued profile and pending delivery handoff')
    elif value['format'] == TRANSLATOR_RECORD:
        proofs = value['business']['live']['evidence_sha256']
        old = prior['business']['live']['evidence_sha256']
        expected = dict(before['realtime'], translations=TRANSLATOR_CAPABILITY)
        ledger.require(prior['format'] == CODE_RECORD and
                       record_hash(prior) == CODE_REVIEWED.get(prior['profile']) and
                       not previous['added'] and
                       set(previous['capabilities']) == {'protocol_source_sha256','realtime'} and
                       ledger.canonical(after['realtime']) == ledger.canonical(expected) and
                       format_for(after) == TRANSLATOR_RECORD and
                       old.keys() | {'translation-transport.json'} == proofs.keys() and
                       ledger.HASH.fullmatch(proofs['translation-transport.json']),
                       'translation qualification needs the issued predecessor and both transports')
    elif value['format'] == CODE_RECORD:
        proofs = value['business']['live']['evidence_sha256']
        old = prior['business']['live']['evidence_sha256']
        ledger.require(prior['format'] == SKILLS_RECORD and
                       record_hash(prior) == SKILLS_REVIEWED.get(prior['profile']) and
                       previous['added'] == ['hosted_python'] and
                       set(previous['capabilities']) == {'protocol_source_sha256'} and
                       format_for(after) == CODE_RECORD and old.keys() | {'code-handoff.json'} == proofs.keys() and
                       ledger.HASH.fullmatch(proofs['code-handoff.json']),
                       'interpreter qualification requires the selected workspace record and SDK handoff')
    elif value['format'] == SKILLS_RECORD:
        proofs = value['business']['live']['evidence_sha256']
        old = prior['business']['live']['evidence_sha256']
        ledger.require(prior['format'] == ASSISTANTS_RECORD and
                       record_hash(prior) == ASSISTANTS_REVIEWED.get(prior['profile']) and
                       previous['added'] == ['container_workspaces'] and
                       set(previous['capabilities']) == {'protocol_source_sha256'} and
                       format_for(after) == SKILLS_RECORD and
                       old.keys() | {'skill-handoff.json'} == proofs.keys() and
                       ledger.HASH.fullmatch(proofs['skill-handoff.json']),
                       'workspace qualification requires the released predecessor and skill handoff')
    elif value['format'] == ASSISTANTS_RECORD:
        proofs = value['business']['live']['evidence_sha256']
        old_proofs = prior['business']['live']['evidence_sha256']
        ledger.require(prior['format'] == TRANSLATION_RECORD and
                       record_hash(prior) == TRANSLATION_REVIEWED.get(prior['profile']) and
                       previous['added'] == ['assistants'] and
                       set(previous['capabilities']) == {'protocol_source_sha256'} and
                       format_for(after) == ASSISTANTS_RECORD and
                       old_proofs.keys() | {'assistant-handoff.json'} == proofs.keys() and
                       ledger.HASH.fullmatch(proofs['assistant-handoff.json']),
                       'Assistants qualification requires the reviewed predecessor and SDK handoff')
    elif value['format'] == TRANSLATION_RECORD:
        proofs = value['business']['live']['evidence_sha256']
        old_proofs = prior['business']['live']['evidence_sha256']
        additions = {'translations.json', 'beta-conversation.json', 'beta-transcription.json'}
        ledger.require(prior['format'] == SUBAGENT_RECORD and
                       record_hash(prior) in SUBAGENT_REVIEWED.get(prior['profile'], ()) and
                       previous['added'] == ['translations'] and
                       set(previous['capabilities']) == {'protocol_source_sha256'} and
                       format_for(after) == TRANSLATION_RECORD and
                       old_proofs.keys() | additions == proofs.keys() and
                       all(ledger.HASH.fullmatch(proofs[k]) for k in additions),
                       'translation qualification requires the reviewed predecessor and three SDK receipts')
    elif value['format'] == SUBAGENT_RECORD:
        proofs = value['business']['live']['evidence_sha256']
        old_proofs = prior['business']['live']['evidence_sha256']
        ledger.require(prior['format'] == MANAGED_TOOL_RECORD
                       and record_hash(prior) == MANAGED_TOOL_REVIEWED.get(prior['profile'])
                       and previous['added'] == ['agent_session_subagents']
                       and set(previous['capabilities']) == {'protocol_source_sha256'}
                       and format_for(after) == SUBAGENT_RECORD
                       and old_proofs.keys() | {'subagent-handoff.json'} == proofs.keys()
                       and ledger.HASH.fullmatch(proofs['subagent-handoff.json']),
                       'subagent qualification requires the reviewed tool predecessor and a new child handoff')
    elif value['format'] == MANAGED_TOOL_RECORD:
        proofs = value['business']['live']['evidence_sha256']
        old_proofs = prior['business']['live']['evidence_sha256']
        ledger.require(prior['format'] == ARTIFACT_RECORD and
                       record_hash(prior) == ARTIFACT_REVIEWED.get(prior['profile']) and
                       not previous['added'] and
                       set(previous['capabilities']) == {'protocol_source_sha256', 'agent_sessions'} and
                       format_for(after) == MANAGED_TOOL_RECORD and old_proofs.keys() <= proofs.keys() and
                       proofs['session-handoff.json'] != old_proofs['session-handoff.json'],
                       'managed tools require a qualified artifact predecessor and fresh tool handoff')
    elif value['format'] == ARTIFACT_RECORD:
        proofs = value['business']['live']['evidence_sha256']
        ledger.require(prior['format'] == WORKSPACE_RECORD and
                       record_hash(prior) in WORKSPACE_REVIEWED.get(prior['profile'], ()) and
                       previous['added'] == ['agent_session_artifacts'] and
                       set(previous['capabilities']) == {'protocol_source_sha256'} and
                       format_for(after) == ARTIFACT_RECORD and
                       set(prior['business']['live']['evidence_sha256']) <= set(proofs) and
                       proofs['session-handoff.json'] != prior['business']['live']['evidence_sha256']['session-handoff.json'],
                       'artifacts require the qualified workspace and a fresh file handoff')
    elif workspace:
        proofs = value['business']['live']['evidence_sha256']
        ledger.require(prior['format'] == ENVIRONMENT_RECORD and
                       record_hash(prior) == ENVIRONMENT_REVIEWED.get(prior['profile']) and
                       previous['added'] == ['agent_environments'] and
                       set(previous['capabilities']) == {'protocol_source_sha256', 'agent_sessions'} and
                       format_for(after) == WORKSPACE_RECORD and
                       ledger.canonical(before['agent_sessions']) == ledger.canonical(OAUTH_SESSION) and
                       {'oauth-handoff.json', 'vault-handoff.json', 'registration.json',
                        'interruption.json', 'session-handoff.json'} <= proofs.keys() and
                       proofs['session-handoff.json'] != prior['business']['live']['evidence_sha256']['session-handoff.json'],
                       'workspace requires the reviewed template predecessor and a new session handoff')
    elif templates:
        evidence = value['business']['live']['evidence_sha256']
        ledger.require(prior['format'] == OAUTH_RECORD and record_hash(prior) == OAUTH_REVIEWED.get(prior['profile'])
                       and previous['added'] == ['agent_environment_templates', 'agent_session_traces']
                       and set(previous['capabilities']) == {'protocol_source_sha256'}
                       and format_for(after) == ENVIRONMENT_RECORD
                       and {'oauth-handoff.json', 'vault-handoff.json', 'session-handoff.json',
                            'registration.json', 'interruption.json'} <= evidence.keys()
                       and evidence['session-handoff.json'] != prior['business']['live']['evidence_sha256']['session-handoff.json'],
                       'templates and traces need the reviewed OAuth predecessor and a fresh cross-capacity handoff')
    elif oauth:
        ledger.require(prior['format'] == VAULT_RECORD and
                       record_hash(prior) == VAULT_REVIEWED.get(prior['profile']) and
                       not previous['added'] and
                       set(previous['capabilities']) == {'protocol_source_sha256', 'agent_sessions'} and
                       format_for(after) == OAUTH_RECORD and
                       {'oauth-handoff.json', 'vault-handoff.json', 'session-handoff.json',
                        'registration.json', 'interruption.json'} <= value['business']['live']['evidence_sha256'].keys(),
                       'OAuth qualification requires the accepted Vault predecessor and actual renewal')
    elif vault:
        ledger.require(prior['format'] == SESSION_RECORD and
                       record_hash(prior) == SESSION_REVIEWED.get(prior['profile']) and
                       previous['added'] == ['agent_vaults'] and
                       set(previous['capabilities']) == {'protocol_source_sha256', 'agent_sessions'} and
                       format_for(after) == VAULT_RECORD and
                       {'vault-handoff.json', 'session-handoff.json', 'registration.json',
                        'interruption.json'} <= value['business']['live']['evidence_sha256'].keys(),
                       'Vault qualification needs the accepted session and real credential handoff')
    elif registration:
        from tools.serve_recovery import profile_duplex_reuse
        ledger.require(prior['format'] == AGENT_RECORD_FORMAT and
                       record_hash(prior) == REFRESH_REVIEWED.get(prior['profile']) and
                       previous['added'] == ['agent_registration'] and
                       set(previous['capabilities']) == {'protocol_source_sha256'} and
                       value['capabilities']['agent_registration'] == REGISTRATION_CAPABILITY and
                       {'interruption.json','registration.json'} <=
                       value['business']['live']['evidence_sha256'].keys(),
                       'registration must extend the qualified recovery and SDK business')
        value['business']['realtime_duplex'] = profile_duplex_reuse.pack_text(
            value['business']['realtime_duplex'])
    elif session:
        ledger.require(prior['format'] == REGISTRATION_RECORD and
                       record_hash(prior) == REGISTRATION_REVIEWED.get(prior['profile']) and
                       previous['added'] == ['agent_sessions'] and
                       set(previous['capabilities']) == {'protocol_source_sha256'} and
                       after['agent_sessions'] == SESSION_CAPABILITY and
                       {'session-handoff.json','registration.json','interruption.json'} <=
                       value['business']['live']['evidence_sha256'].keys(),
                       'session qualification needs retained registration, recovery and a real handoff')
    elif refresh:
        ledger.require(record_hash(prior) == AGENT_REVIEWED.get(prior['profile']) and
                       not previous['added'] and
                       set(previous['capabilities']) == {'protocol_source_sha256'},
                       'requalification must preserve the reviewed agent capabilities')
    elif revision:
        ledger.require(prior['format'] == (BETA_RECORD_FORMAT if agent else RECORD_FORMAT),
                       'new Responses qualification requires its accepted predecessor')
    if revision:
        previous['live'] = prior['business']['live']
    if agent and not refresh:
        original = dict(after['responses'])
        original.pop('multi_agent')
        ledger.require(original == before['responses'], 'unreviewed Responses capability difference')
        del previous['capabilities']['responses']
    value['reused_business'] = dict(format=AGENT_REFRESH if refresh else REUSE_FORMAT[value['format']],
                                    previous=codec.pack(previous))
    ledger.require(record_hash(previous_record(value)) == record_hash(prior),
                   'Live qualification cannot reconstruct its accepted source')


def previous_record(value):
    reuse = value['reused_business']
    ledger.keys(reuse, ('format', 'previous'))
    agent = value['format'] == AGENT_RECORD_FORMAT
    refresh = agent and reuse['format'] == AGENT_REFRESH
    registration = value['format'] == REGISTRATION_RECORD
    session = value['format'] == SESSION_RECORD
    revision = value['format'] in REUSE_FORMAT and value['format'] != RECORD_FORMAT
    ledger.require(refresh or reuse['format'] == REUSE_FORMAT.get(value['format']),
                   'incorrect Live qualification source')
    previous = codec.unpack(reuse['previous'])
    fields = (*METADATA, 'engine', 'ctimes', 'capabilities', 'added')
    ledger.keys(previous, (*fields, 'live') if revision else fields)
    original = copy.deepcopy(value)
    original.update({k: previous[k] for k in METADATA})
    original['identity'].update(previous['engine'])
    for name, ctime in previous['ctimes'].items():
        original['identity']['model_assets'][name]['ctime_ns'] = ctime
    for name in previous['added']:
        del original['capabilities'][name]
    original['capabilities'].update(previous['capabilities'])
    if agent and not refresh:
        del original['capabilities']['responses']['multi_agent']
    if registration:
        from tools.serve_recovery import profile_duplex_reuse
        original['business']['realtime_duplex'] = profile_duplex_reuse.unpack_text(
            original['business']['realtime_duplex'])
    if revision:
        original['business']['live'] = previous['live']
    else:
        del original['business']['live']
    return original


def validate(value):
    from tools.serve_recovery import profile_trace_reuse
    if value['format'] in (HOSTED_LIFECYCLE_RECORD, WEBHOOK_RECORD, AUTOMATION_RECORD):
        current, previous = {
            HOSTED_LIFECYCLE_RECORD: (HOSTED_LIFECYCLE_REVIEWED, WEBHOOK_REVIEWED),
            WEBHOOK_RECORD: (WEBHOOK_REVIEWED, AUTOMATION_REVIEWED),
            AUTOMATION_RECORD: (AUTOMATION_REVIEWED, TRANSLATOR_REVIEWED),
        }[value['format']]
        ledger.require(record_hash(value) == current.get(value['profile']),
                       'profile differs from the qualified delivery handoff')
        prior = previous_record(value)
        ledger.require(record_hash(prior) == previous.get(value['profile']),
                       'delivery handoff lost its issued predecessor')
        # Reconstruct the allowed migration from the retained predecessor.
        # The profile registry already binds this complete record by digest.
        reproduced = copy.deepcopy(value)
        attach(reproduced, prior)
        ledger.require(ledger.canonical(reproduced['reused_business']) ==
                       ledger.canonical(value['reused_business']),
                       'delivery handoff predecessor differs')
        validate(prior)
        return value['reused_business']
    if value['format'] == TRANSLATOR_RECORD:
        name = value['profile']
        ledger.require(record_hash(value) == TRANSLATOR_REVIEWED.get(name),
                       'translation record differs from its qualification')
        prior = previous_record(value)
        ledger.require(record_hash(prior) == CODE_REVIEWED.get(name),
                       'translation transport lost its issued predecessor')
        validate(prior)
        return value['reused_business']
    if value['format'] == CODE_RECORD:
        name = value['profile']
        ledger.require(record_hash(value) == CODE_REVIEWED.get(name),
                       'interpreter record differs from its qualification')
        prior = previous_record(value)
        ledger.require(record_hash(prior) == SKILLS_REVIEWED.get(name),
                       'interpreter qualification lost its released predecessor')
        validate(prior)
        return value['reused_business']
    if value['format'] == SKILLS_RECORD:
        name = value['profile']
        ledger.require(record_hash(value) == SKILLS_REVIEWED.get(name),
                       'skill workspace profile differs from its qualification')
        prior = previous_record(value)
        ledger.require(record_hash(prior) == ASSISTANTS_REVIEWED.get(name),
                       'skill workspace qualification lost its released predecessor')
        validate(prior)
        return value['reused_business']
    if value['format'] == ASSISTANTS_RECORD:
        name = value['profile']
        ledger.require(record_hash(value) == ASSISTANTS_REVIEWED.get(name),
                       'Assistants qualification differs from the reviewed capacity')
        prior = previous_record(value)
        ledger.require(record_hash(prior) == TRANSLATION_REVIEWED.get(name),
                       'Assistants qualification lost its released predecessor')
        validate(prior)
        return value['reused_business']
    if value['format'] == TRANSLATION_RECORD:
        name = value['profile']
        ledger.require(record_hash(value) == TRANSLATION_REVIEWED.get(name),
                       'translation qualification differs from the reviewed run')
        prior = previous_record(value)
        ledger.require(record_hash(prior) in SUBAGENT_REVIEWED.get(name, ()),
                       'translation qualification lost the reviewed predecessor')
        validate(prior)
        return value['reused_business']
    if value['format'] == SUBAGENT_RECORD:
        name = value['profile']
        ledger.require(record_hash(value) in SUBAGENT_REVIEWED.get(name, ()),
                       'subagent qualification differs from the reviewed capacity handoff')
        prior = previous_record(value)
        ledger.require(record_hash(prior) == MANAGED_TOOL_REVIEWED.get(name),
                       'subagent qualification lost the managed tool predecessor')
        validate(prior)
        return value['reused_business']
    if value['format'] == MANAGED_TOOL_RECORD:
        name = value['profile']
        ledger.require(record_hash(value) == MANAGED_TOOL_REVIEWED.get(name),
                       'managed tools differ from the reviewed capacity handoff')
        prior = previous_record(value)
        ledger.require(record_hash(prior) == ARTIFACT_REVIEWED.get(name),
                       'managed tool qualification lost the artifact predecessor')
        validate(prior)
        return value['reused_business']
    if value['format'] == ARTIFACT_RECORD:
        name = value['profile']
        ledger.require(record_hash(value) == ARTIFACT_REVIEWED.get(name),
                       'artifact qualification differs from reviewed primary/light evidence')
        prior = previous_record(value)
        ledger.require(record_hash(prior) in WORKSPACE_REVIEWED.get(name, ()),
                       'artifact qualification lost the hosted workspace predecessor')
        validate(prior)
        return value['reused_business']
    if value['format'] == WORKSPACE_RECORD:
        name = value['profile']
        ledger.require(record_hash(value) in WORKSPACE_REVIEWED.get(name, ()),
                       'workspace differs from the reviewed handoff')
        prior = previous_record(value)
        ledger.require(record_hash(prior) == ENVIRONMENT_REVIEWED.get(name),
                       'workspace lost the reviewed template predecessor')
        validate(prior)
        return value['reused_business']
    if value['format'] == ENVIRONMENT_RECORD:
        name = value['profile']
        ledger.require(record_hash(value) == ENVIRONMENT_REVIEWED.get(name),
                       'template and session trace qualification differs from the reviewed handoff')
        prior = previous_record(value)
        ledger.require(record_hash(prior) == OAUTH_REVIEWED.get(name),
                       'template qualification lost the OAuth predecessor')
        validate(prior)
        return value['reused_business']
    if value['format'] == OAUTH_RECORD:
        name = value['profile']
        ledger.require(record_hash(value) == OAUTH_REVIEWED.get(name),
                       'OAuth qualification differs from the reviewed credential exchange')
        prior = previous_record(value)
        ledger.require(record_hash(prior) == VAULT_REVIEWED.get(name),
                       'OAuth qualification lost the Vault predecessor')
        validate(prior)
        return value['reused_business']
    if value['format'] == VAULT_RECORD:
        name = value['profile']
        ledger.require(record_hash(value) == VAULT_REVIEWED.get(name),
                       'Vault handoff differs from the reviewed run')
        prior = previous_record(value)
        ledger.require(record_hash(prior) == SESSION_REVIEWED.get(name),
                       'Vault qualification lost the session predecessor')
        validate(prior)
        return value['reused_business']
    if value['format'] == SESSION_RECORD:
        name = value['profile']
        ledger.require(record_hash(value) == SESSION_REVIEWED.get(name),
                       'session handoff differs from the reviewed qualification')
        prior = previous_record(value)
        ledger.require(record_hash(prior) == REGISTRATION_REVIEWED.get(name),
                       'session handoff lost the registered predecessor')
        validate(prior)
        return value['reused_business']
    if value['format'] == REGISTRATION_RECORD:
        name = value['profile']
        ledger.require(record_hash(value) == REGISTRATION_REVIEWED.get(name),
                       'Agent registration differs from its reviewed qualification')
        prior = previous_record(value)
        ledger.require(record_hash(prior) == REFRESH_REVIEWED.get(name),
                       'Agent registration lost the qualified interruption recovery')
        validate(prior)
        return value['reused_business']
    if value['format'] == AGENT_RECORD_FORMAT:
        name = value['profile']
        if value['reused_business'].get('format') == AGENT_REFRESH:
            ledger.require(record_hash(value) == REFRESH_REVIEWED.get(name),
                           'agent recovery differs from the reviewed profile')
            prior = previous_record(value)
            ledger.require(record_hash(prior) == AGENT_REVIEWED.get(name),
                           'agent recovery lost the registered hosted business')
            validate(prior)
            return value['reused_business']
        ledger.require(record_hash(value) == AGENT_REVIEWED.get(name),
                       'hosted agent qualification differs from the reviewed run')
        prior = previous_record(value)
        ledger.require(prior['format'] == BETA_RECORD_FORMAT and
                       record_hash(prior) == BETA_REVIEWED[name],
                       'hosted agent qualification lost the registered Beta source')
        validate(prior)
        return value['reused_business']
    if value['format'] == BETA_RECORD_FORMAT:
        name = value['profile']
        ledger.require(record_hash(value) == BETA_REVIEWED.get(name),
                       'Beta qualification differs from reviewed business and run identities')
        prior = previous_record(value)
        ledger.require(prior['format'] == RECORD_FORMAT and
                       record_hash(prior) == REVIEWED[name]['selected'],
                       'Beta qualification lost its accepted Live source')
        validate(prior)
        return value['reused_business']
    expected = REVIEWED.get(value['profile'])
    ledger.require(expected and value['format'] == RECORD_FORMAT and
                   record_hash(value) == expected['selected'],
                   'Live qualification differs from reviewed business and run identities')
    previous = previous_record(value)
    ledger.require(record_hash(previous) == expected['previous'],
                   'Live qualification lost historical business or its original failures')
    profile_trace_reuse.validate(previous)
    return value['reused_business']

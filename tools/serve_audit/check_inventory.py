#!/usr/bin/env python3
"""Fail when serve's reviewed surface drifts; does not certify its semantics."""
import argparse
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
INVENTORY = ROOT / "tools/serve_audit/inventory.json"


def native_token_observer(generation):
    """Review the actual fast-path gate, not only the final output adapter."""
    body = generation.split('const auto consume_token =', 1)[1].split(
        'std::vector<int32_t> generated;', 1)[0]
    gates = re.findall(r'if\s*\((.*?)\)\s*return true;', body, re.DOTALL)
    buffered = [gate for gate in gates if '!responses' in gate]
    if len(buffered) != 1 or not re.search(r'!native\s*&&', buffered[0]) or \
            'native->Emit(delta)' not in generation:
        raise ValueError('native output skipped token observation')


def native_live_binding(realtime, live):
    """Review concrete primary ownership; wire shapes alone are not execution."""
    if 'HandleLive(fd, path, headers, initial, accept)' not in realtime or \
            'path.substr(0, path.find(\'?\')) == "/v1/live/sessions"' not in realtime or \
            not all(marker in live for marker in (
                'path != "/v1/live/sessions"', 'PrepareRealtimeInput(client)',
                'clients_.RecoveryIdentity(client.Index())',
                'clients_.OwnsIdentity(client)', 'identity->Limits()',
                'LockUnlessCancelled(tok_mu_, cancelled)', 'tok_->Encode(',
                'MapLiveStartupHistory(', 'PrepareChatPrompt(',
                'PlanNativeLiveClient(', 'requests_.Register(',
                'requests_.Remove(control)',
                'RunRealtimeConversation(client, job, control, delta, speech)',
                'std::make_unique<NativeLiveClientBackend>',
                'clients_.Authenticate(headers.client_id, headers.gateway_token)',
                'return session.Finalized()', 'session.Join()')):
        raise ValueError('review native Live primary execution and ownership')


def missing_protocol_sources(root, manifest=None):
    if manifest is None:
        manifest = (root / "cmake/ChatProtocolIdentity.cmake").read_text()
    registered = set(re.findall(
        r"^\s*((?:src/server|include/q4t/server|src/text|include/q4t/text)/[^\s()]+\.(?:h|cpp))(?=\s|\))",
        manifest, re.MULTILINE))
    actual = {str(path.relative_to(root))
              for directory in ("src/server", "include/q4t/server",
                                "src/text", "include/q4t/text")
              for path in (root / directory).glob("*")
              if path.suffix in (".h", ".cpp")}
    return sorted(actual - registered)


def current_surface(root):
    missing = missing_protocol_sources(root)
    if missing:
        raise ValueError("server files missing from protocol identity: " + str(missing))
    contract = (root / "src/server/chat_contract.cpp").read_text()
    match = re.search(r'Fields\(req,\s*\{([^}]+)\}', contract)
    if not match:
        raise ValueError("cannot extract Chat allowlist; review extractor")
    fields = re.findall(r'"([a-z_]+)"', match[1])
    completion = (root / "src/server/completion_plan.cpp").read_text()
    match = re.search(r'InputFields\(\s*req,\s*\{([^}]+)\}', completion)
    if not match:
        raise ValueError("cannot extract Completions allowlist; review extractor")
    completion_fields = re.findall(r'"([a-z_]+)"', match[1])
    dispatch = (root / "src/server/chat_http.cpp").read_text()
    routes = re.findall(r'path == "([^"]+)" && method == "([A-Z]+)"', dispatch)
    if 'HandleTranscription(fd, headers.content_type, body,' in dispatch:
        reader = (root / 'src/server/http_request.cpp').read_text()
        if 'IsTranscriptionTarget(path)' not in dispatch or \
                'IsTranscriptionTarget(*path) ? kTranscriptionBodyBytes' not in reader or \
                'headers.body_limit, kAsrFormBytes' not in dispatch:
            raise ValueError('review native transcription body/dispatch contract')
        if 'path == "/v1/audio/translations"' not in dispatch:
            raise ValueError('review native audio translation execution')
        routes.extend((path, "POST") for path in
                      ("/v1/audio/transcriptions", "/v1/audio/translations"))
    if 'if (IsModelDetailPath(path))' not in dispatch or \
            'method != "GET" && method != "DELETE"' not in dispatch or \
            'HandleModel(fd, method, path)' not in dispatch:
        raise ValueError("review dynamic model backend route extractor")
    routes.extend(("/v1/models/{model}", method) for method in ("GET", "DELETE"))
    if 'HandleRealtime(fd, path, headers, state->pending, websocket_accept)' in dispatch:
        realtime = (root / 'src/server/chat_realtime.cpp').read_text()
        if 'IsRealtimeTarget(path)' not in dispatch or 'CheckWebSocketUpgrade(' not in dispatch or \
                'RunRealtimeTranscription(client, job, pcm, control, emit)' not in realtime or \
                'ClientLane::kData' not in realtime or \
                not re.search(r'\brate\.Reserve\(\s*clients_,\s*client\b', realtime):
            raise ValueError('review realtime upgrade/native execution/per-turn admission')
        routes.append(("/v1/realtime?intent=transcription", "GET"))
        translation = (root / 'src/server/chat_realtime_translation.cpp').read_text()
        if ('HandleRealtimeTranslation(fd, path, headers, initial, accept)' in realtime
                and 'RunRealtimeTranscription(client, audio, pcm, control, source_delta)' in translation
                and re.search(r'RunRealtimeConversation\(client, job, control, text, speech,\s*/\*temperature=\*/0\.0\)', translation)):
            routes.append(("/v1/realtime/translations", "GET"))
        if 'HandleRealtimeConversation(fd, path, headers, initial, accept)' in realtime:
            native_token_observer((root / 'src/server/chat_generation.cpp').read_text())
            conversation = (root / 'src/server/chat_realtime_conversation.cpp').read_text()
            execution = (root / 'src/server/realtime_chat_execution.cpp').read_text()
            if not all(marker in conversation for marker in (
                    'ParseRealtimeChatTarget(', 'PlanRealtimeChat(',
                    'FitRealtimeChatContext(', 'ExecuteRealtimeChat(',
                    'ClientLane::kData', 'requests_.Register(',
                    'HandleChat(-1, {}, GenerationApi::kChat')) or \
                    'ClaimInvocation()' not in execution or \
                    'ClientRateDomain::kBatch' in execution:
                raise ValueError('review Realtime conversation native ownership')
            routes.append(("/v1/realtime", "GET"))
        if 'HandleLive(fd, path, headers, initial, accept)' in realtime:
            live = (root / 'src/server/chat_live.cpp').read_text()
            native_live_binding(realtime, live)
            routes.append(("/v1/live/sessions", "GET"))
    native = (root / "src/service/ingress_main.cpp").read_text()
    native_routes = re.findall(
        r'\{\s*"(GET|POST)"\s*,\s*"([^"]+)"\s*,\s*(true|false)\s*\}', native)
    if len(native_routes) < 5:
        raise ValueError("cannot extract native ingress routes; review extractor")
    if 'server::IsModelDetailPath(path)' not in native or \
            'method == "GET" || method == "DELETE"' not in native:
        raise ValueError("review dynamic model native route extractor")
    native_routes.extend((method, "/v1/models/{model}", "true")
                         for method in ("GET", "DELETE"))
    application_routes = (root / "include/q4t/server/application_routes.h").read_text()
    if 'HandleRealtimeTraceRequest(' not in dispatch or \
            'IsRealtimeTraceTarget(path)' not in dispatch or \
            'server::RealtimeTraceMethodAllowed(method, path)' not in native or \
            'inline bool RealtimeTraceMethodAllowed' not in application_routes:
        raise ValueError('review native trace transport and authenticated scope')
    if 'SendLiveRecording(fd, *objects_, access, std::string(id))' not in dispatch or \
            'server::LiveRecordingId(path)' not in native or 'inline std::string_view LiveRecordingId' not in application_routes:
        raise ValueError('review authenticated binary Live content routing')
    content = '/v1/live/sessions/{session_id}/content'
    routes.append((content, 'GET'))
    native_routes.append(('GET', content, 'false'))
    attachment = '/v1/live/sessions/{session_id}/attach'
    sideband = (root / 'src/server/chat_live_sideband.cpp').read_text()
    if ('HandleLiveSideband(fd, path, headers, initial, accept)' not in realtime or
            'lease.sideband = (method == "GET" && server::IsCallSideband(path))' not in native or
            not all(marker in sideband for marker in
                    ('slot.client == client.Id()', 'slot.session == id', 'stream->Attach(',
                     'stream->Next(', 'stream->Enqueue(', 'RunWebSocketSession('))):
        raise ValueError('review Live attach authorization, replay and command ownership')
    routes.append((attachment, 'GET'))
    native_routes.append(('GET', attachment, 'false'))
    media = (root / 'src/server/chat_live_media.cpp').read_text()
    if ('HandleLiveMediaCreate(fd, LiveForkId(path), body, admission)' not in dispatch or
            '!server::LiveForkId(path).empty()' not in native or
            'ReadLiveForkState(*objects_, access, std::string(source))' not in live or
            not all('LoadLiveFork(fd, client, source, &state)' in unit for unit in (live, media))):
        raise ValueError('review owner-scoped fork dispatch and checkpoint loading')
    if ('HandleLiveMediaCreate(fd, {}, body, admission, {}, headers.content_type,' not in dispatch or
            'IsTranslationCall(path));' not in dispatch or
            'return target == "/v1/realtime/calls" || IsTranslationCall(target);' not in application_routes or
            'owner->call = std::make_unique<TranslationMediaCall>(' not in media or
            'slot->attachable.store(!sip && !translation);' not in media or
            'ParseRealtimeWebRtcOffer(*offer_type, body, &offer, translation)' not in media or
            'slot.session != id || slot.client != http->Client().Id()' not in media or
            'RealtimeCarrier::kWebRtcData' not in media):
        raise ValueError('review SDP negotiation, data channel and call ownership')
    issuer = (root / 'src/server/chat_realtime_secrets.cpp').read_text()
    if ('IssueRealtimeSecret(fd, headers, body)' not in dispatch or
            'grant.owner != client.Id()' not in dispatch or
            'realtime_secrets_.Resolve(' not in dispatch or
            'PreviewCredential(startup, &resource)' not in issuer or
            'realtime_secrets_.Issue({client.Id(), headers.gateway_token, issuer, legacy_temperature}' not in issuer or
            'LegacyConversationCredential(request, &request, &temperature)' not in issuer or
            'context.SetRealtimeStartup(&grant.session, grant.legacy_temperature)' not in dispatch or
            'access.AuthenticateRealtime(ticket, binding.token, now)' not in native or
            'access.StillActive(identity)' not in native):
        raise ValueError('review Realtime credential issuing key, verified owner and allowed targets')
    routes.extend((path, 'POST') for path in ('/v1/realtime/calls', '/v1/realtime/translations/calls'))
    routes.append(('/v1/live/sessions', 'POST'))
    fork = '/v1/live/sessions/{session_id}/fork'
    for method in ('GET', 'POST'):
        routes.append((fork, method))
        native_routes.append((method, fork, 'false'))
    reject = '/v1/live/sessions/{session_id}/reject'
    if ('HandleSipReject(fd, SipRejectId(path), body)' not in dispatch or
            'server::IsSipReject(method, path)' not in native or
            'sip_service_.Reject(http->Client().Id()' not in media):
        raise ValueError('review scoped SIP rejection and native routing')
    for path in (reject, '/v1/realtime/calls/{call_id}/reject'):
        routes.append((path, 'POST'))
        native_routes.append(('POST', path, 'true'))
    if ('SipControlId(path, "/accept")' not in dispatch or
            'RealtimeMediaCall' not in media or
            'server::SipControlId(path, "/accept")' not in native or
            'sip_service_.Reserve(client.Id(), sip_id' not in media or
            'server::IsSipRefer(method, path)' not in native or
            'sip_service_.Refer(http->Client().Id()' not in media or
            'server::IsSipHangup(method, path)' not in native or
            'sip_service_.Hangup(http->Client().Id()' not in media):
        raise ValueError('review authenticated SIP accept, hangup and routing')
    for path, control in (('/v1/live/sessions/{session_id}/accept', 'false'),
                          ('/v1/realtime/calls/{call_id}/accept', 'false'),
                          ('/v1/live/sessions/{session_id}/hangup', 'true'),
                          ('/v1/realtime/calls/{call_id}/hangup', 'true'),
                          ('/v1/live/sessions/{session_id}/refer', 'true'),
                          ('/v1/realtime/calls/{call_id}/refer', 'true')):
        routes.append((path, 'POST'))
        native_routes.append(('POST', path, control))
    traces = [("GET", "/v1/realtime/traces"),
              ("GET", "/v1/realtime/traces/{trace_id}"),
              ("DELETE", "/v1/realtime/traces/{trace_id}")]
    routes.extend((path, method) for method, path in traces)
    native_routes.extend((method, path, "true") for method, path in traces)
    if 'HandleTranscription(fd, headers.content_type, body,' in dispatch:
        if 'path == "/v1/files" || IsTranscriptionTarget(target)' not in application_routes or \
                'server::IsTranscriptionTarget(path)' not in native or \
                'std::min(headers.body_limit, server::kTranscriptionBodyBytes)' not in native or \
                'upload.max_bytes = headers.body_limit' not in native:
            raise ValueError('review native transcription upload quotas')
    if 'IsConversationTarget(path)' not in dispatch or \
            'HandleConversationRequest(*objects_' not in dispatch or \
            'server::ConversationMethodAllowed(method, path)' not in native or \
            'inline bool ConversationMethodAllowed' not in application_routes:
        raise ValueError("review dynamic Conversations transport extractor")
    if 'HandleVoices(fd, method, path, headers.content_type, body)' not in dispatch or \
            'server::VoiceMethodAllowed(method, path)' not in native or \
            'server::IsVoiceMetadata(method, path)' not in native or \
            'std::min(headers.body_limit, server::kVoiceFormBytes)' not in native or \
            not re.search(r'IsVoiceTarget\(\*path\)\s*\?\s*kVoiceFormBytes', reader) or \
            'IsVoiceCreation(method, target)' not in application_routes or \
            'inline bool VoiceMethodAllowed' not in application_routes:
        raise ValueError("review native Voice/Consent dispatch, scope and quotas")
    voices = [("POST", "/v1/audio/voices"),
              ("DELETE", "/v1/audio/voices/{voice_id}"),
              *((method, "/v1/audio/voice_consents") for method in ("GET", "POST")),
              *((method, "/v1/audio/voice_consents/{consent_id}")
                for method in ("GET", "POST", "DELETE"))]
    routes.extend((path, method) for method, path in voices)
    native_routes.extend((method, path, "false" if method == "POST" and
        path in ("/v1/audio/voices", "/v1/audio/voice_consents") else "true")
        for method, path in voices)
    conversations = [
        ("POST", "/v1/conversations"),
        *((method, "/v1/conversations/{conversation_id}")
          for method in ("GET", "POST", "DELETE")),
        *((method, "/v1/conversations/{conversation_id}/items")
          for method in ("GET", "POST")),
        *((method, "/v1/conversations/{conversation_id}/items/{item_id}")
          for method in ("GET", "DELETE"))]
    routes.extend((path, method) for method, path in conversations)
    native_routes.extend((method, path, "false") for method, path in conversations)
    if 'ServeResponsesState(fd, *objects_' not in dispatch or \
            'server::ResponsesMethodAllowed(method, path)' not in native or \
            'inline bool ResponsesMethodAllowed' not in application_routes:
        raise ValueError("review dynamic Responses transport extractor")
    responses = [("GET", "/v1/responses/{response_id}"),
                 ("DELETE", "/v1/responses/{response_id}"),
                 ("POST", "/v1/responses/{response_id}/cancel"),
                 ("GET", "/v1/responses/{response_id}/input_items")]
    routes.extend((path, method) for method, path in responses)
    if 'server::IsResponsesObservation(method, path)' not in native or \
            'server::IsCancellationRequest(method, path)' not in native or \
            'path.substr(end) == "/cancel"' not in application_routes:
        raise ValueError("review Responses cancellation/control route extractor")
    native_routes.append(("POST", "/v1/responses", "false"))
    if 'if (IsResponsesCompaction(path))' not in dispatch or \
            'if (IsResponsesCompaction(path)) return method == "POST"' not in application_routes:
        raise ValueError("review native compact transport extractor")
    routes.append(("/v1/responses/compact", "POST"))
    native_routes.append(("POST", "/v1/responses/compact", "false"))
    if 'if (IsResponsesTokenCount(path))' not in dispatch or \
            'if (IsResponsesTokenCount(path)) return method == "POST"' not in application_routes:
        raise ValueError("review native input token count transport extractor")
    routes.append(("/v1/responses/input_tokens", "POST"))
    native_routes.append(("POST", "/v1/responses/input_tokens", "false"))
    native_routes.extend((method, path, "true") for method, path in responses)
    if 'HandleStoredChatRequest(*objects_' not in dispatch or \
            'server::StoredChatMethodAllowed(method, path)' not in native or \
            'server::IsStoredChatManagement(method, path)' not in native or \
            'inline bool StoredChatMethodAllowed' not in application_routes:
        raise ValueError("review dynamic stored Chat transport extractor")
    chats = [("GET", "/v1/chat/completions"),
             *((method, "/v1/chat/completions/{completion_id}")
               for method in ("GET", "POST", "DELETE")),
             ("GET", "/v1/chat/completions/{completion_id}/messages")]
    routes.extend((path, method) for method, path in chats)
    native_routes.extend((method, path, "true") for method, path in chats)
    if 'HandlePromptRequest(*objects_' not in dispatch or \
            'server::PromptMethodAllowed(method, path)' not in native or \
            'inline bool PromptMethodAllowed' not in application_routes:
        raise ValueError("review dynamic native Prompt registry extractor")
    prompts = [*((method, "/v1/prompts") for method in ("GET", "POST")),
               *((method, "/v1/prompts/{prompt_id}") for method in ("GET", "POST", "DELETE")),
               ("POST", "/v1/prompts/{prompt_id}/versions"),
               ("GET", "/v1/prompts/{prompt_id}/versions/{version}")]
    routes.extend((path, method) for method, path in prompts)
    native_routes.extend((method, path, "true" if method != "POST" else "false")
                         for method, path in prompts)
    if 'HandleAgentRegistration(*objects_' not in dispatch or (
            'server::ManagedAgentMethodAllowed(method, path)' not in native or
            'server::IsManagedAgentObservation(method, path)' not in native):
        raise ValueError("review native agent registration routing")
    if 'HandleAgentVault(*objects_' not in dispatch or 'IsVaultTarget(target)' not in application_routes:
        raise ValueError("review native vault routing")
    if ('ParseVaultRoute(target)' not in application_routes or
            not re.search(r'\breturn CredentialRequest\(\s*store, route,',
                          (root/'src/server/agent_vault.cpp').read_text())):
        raise ValueError("review bounded credential routing")
    if ('HandleEnvironmentTemplate(*objects_' not in dispatch or
            'EnvironmentTemplateMethodAllowed(method, target)' not in application_routes):
        raise ValueError("review native environment template routing")
    if ('HandleAgentAutomation(*objects_' not in dispatch or
            'AutomationMethodAllowed(method, automation)' not in application_routes):
        raise ValueError('review managed automation routing')
    if ('HandleHostedEnvironment(*objects_' not in dispatch or
            'ParseHostedRoute(target)' not in application_routes or
            'environment_detail::WorkspaceFiles(' not in (root/'src/server/agent_environment.cpp').read_text()):
        raise ValueError("review native hosted workspace routing")
    if ('HandleAssistantRegistry(*objects_' not in dispatch or
            'server::AssistantMethodAllowed(method, path)' not in native or
            'context->AssistantsBeta()' not in dispatch):
        raise ValueError('review assistant registry routing and version selector')
    file_http = (root/'src/server/files_http.cpp').read_text()
    if not all(value in source for source,value in (
            (dispatch,'IsContainerTarget(path)'), (file_http,'HandleContainers(objects_'),
            (file_http,'PrepareContainer(content_type, stream)'),
            (native,'server::ContainerMethodAllowed(method, path)'),
            (native,'server::IsContainerObservation(method, path)'))):
        raise ValueError('review native container routing')
    for path, methods in (('/v1/containers', ('GET','POST')),
                          ('/v1/containers/{container_id}', ('GET','DELETE')),
                          ('/v1/containers/{container_id}/files', ('GET','POST')),
                          ('/v1/containers/{container_id}/files/{file_id}', ('GET','DELETE')),
                          ('/v1/containers/{container_id}/files/{file_id}/content', ('GET',))):
        for method in methods:
            routes.append((path,method))
            native_routes.append((method,path,'false' if method=='POST' or path.endswith('/content') else 'true'))
    if not all(value in source for source,value in (
            (dispatch,'IsSkillTarget(path)'), (file_http,'HandleSkills(objects_'),
            (file_http,'PrepareSkill(content_type, stream)'), (native,'server::SkillMethodAllowed(method, path)'),
            (native,'server::IsSkillObservation(method, path)'))):
        raise ValueError('review native skill routing')
    for path, methods in (('/v1/skills', ('GET','POST')),
                          ('/v1/skills/{skill_id}', ('GET','POST','DELETE')),
                          ('/v1/skills/{skill_id}/content', ('GET',)),
                          ('/v1/skills/{skill_id}/versions', ('GET','POST')),
                          ('/v1/skills/{skill_id}/versions/{version}', ('GET','DELETE')),
                          ('/v1/skills/{skill_id}/versions/{version}/content', ('GET',))):
        for method in methods:
            routes.append((path,method))
            native_routes.append((method,path,'false' if method=='POST' or path.endswith('/content') else 'true'))
    if 'HandleAssistantThreads(*objects_' not in dispatch:
        raise ValueError('review assistant thread dispatch')
    if 'ServeAssistantRun(fd, *objects_' not in dispatch:
        raise ValueError('review Assistant run routing')
    if not all(marker in source for source, marker in (
            (dispatch, 'HandleChatKit(*objects_, access'),
            (dispatch, 'ServeChatKitClient(fd, *objects_'),
            (dispatch, 'context->ChatKitBeta()'),
            (native, 'server::ChatKitMethodAllowed(method, path)'),
            (native, 'server::IsChatKitObservation(method, path)'),
            (native, 'server::IsChatKitClient(method, path)'),
            (native, 'server::ClientTicketAudience::kChatKit'))):
        raise ValueError('review ChatKit session, thread and native browser routing')
    for method, path in (
            ('POST', '/v1/chatkit'),
            ('POST', '/v1/chatkit/sessions'),
            ('POST', '/v1/chatkit/sessions/{session_id}/cancel'),
            ('GET', '/v1/chatkit/threads'),
            ('GET', '/v1/chatkit/threads/{thread_id}'),
            ('DELETE', '/v1/chatkit/threads/{thread_id}'),
            ('GET', '/v1/chatkit/threads/{thread_id}/items')):
        routes.append((path, method))
        native_routes.append((method, path, 'false' if path in ('/v1/chatkit', '/v1/chatkit/sessions') else 'true'))
    if ('HandleWebhook(*objects_, access' not in dispatch or
            'server::WebhookMethodAllowed(method, path)' not in native):
        raise ValueError('review webhook management and native ingress routing')
    for path, methods in (('/v1/threads/runs', ('POST',)),
                          ('/v1/threads/{thread_id}/runs', ('GET','POST')),
                          ('/v1/threads/{thread_id}/runs/{run_id}', ('GET','POST')),
                          ('/v1/threads/{thread_id}/runs/{run_id}/cancel', ('POST',)),
                          ('/v1/threads/{thread_id}/runs/{run_id}/submit_tool_outputs', ('POST',)),
                          ('/v1/threads/{thread_id}/runs/{run_id}/steps', ('GET',)),
                          ('/v1/threads/{thread_id}/runs/{run_id}/steps/{step_id}', ('GET',)),
                          ('/v1/threads', ('POST',)),
                          ('/v1/threads/{thread_id}', ('GET', 'POST', 'DELETE')),
                          ('/v1/threads/{thread_id}/messages', ('GET', 'POST')),
                          ('/v1/threads/{thread_id}/messages/{message_id}', ('GET', 'POST', 'DELETE')),
                          ('/v1/assistants', ('GET', 'POST')),
                          ('/v1/assistants/{assistant_id}', ('GET', 'POST', 'DELETE')),
                          ('/v1/webhook_event_types', ('GET',)),
                          ('/v1/webhook_endpoints', ('POST', 'GET')),
                          ('/v1/webhook_endpoints/{webhook_endpoint_id}', ('GET', 'POST', 'DELETE')),
                          ('/v1/webhook_endpoints/{webhook_endpoint_id}/rotate_secret', ('POST',)),
                          ('/v1/webhook_endpoints/{webhook_endpoint_id}/test', ('POST',)),
                          ('/v1/agents/automations', ('GET', 'POST')),
                          ('/v1/agents/automations/{automation_id}', ('GET', 'POST', 'DELETE')),
                          ('/v1/agents/automations/{automation_id}/pause', ('POST',)),
                          ('/v1/agents/automations/{automation_id}/resume', ('POST',)),
                          ('/v1/agents/automations/{automation_id}/invocations', ('GET', 'POST')),
                          ('/v1/agents/automations/{automation_id}/invocations/{invocation_id}', ('GET',)),
                          ('/v1/agents/environments', ('GET', 'POST')),
                          ('/v1/agents/environments/{environment_id}', ('GET',)),
                          ('/v1/agents/environments/{environment_id}/files', ('GET', 'POST')),
                          ('/v1/agents/environments/templates', ('GET', 'POST')),
                          ('/v1/agents/environments/templates/{environment_template_id}', ('GET', 'POST', 'DELETE')),
                          ('/v1/vaults/{vault_id}/credentials', ('GET', 'POST')),
                          ('/v1/vaults/{vault_id}/credentials/{credential_id}', ('GET', 'POST', 'DELETE')),
                          ('/v1/vaults', ('GET', 'POST')),
                          ('/v1/vaults/{vault_id}', ('GET', 'POST', 'DELETE')),
                          ('/v1/agents', ('GET', 'POST')),
                          ('/v1/agents/{agent_id}', ('GET', 'POST', 'DELETE'))):
        for method in methods:
            routes.append((path, method))
            native_routes.append((method, path, 'false' if method == 'POST' else 'true'))
    if 'ServeAgentSession(fd, *objects_' not in dispatch or (
            'ParseAgentSessionRoute(target).kind' not in application_routes or
            'server::IsAgentEventStream(method, path)' not in native or
            'server::IsAgentEventSubmission(method, path)' not in native):
        raise ValueError("review native session routing")
    if ('agent_detail::ReadSessionSubagents(objects, route, options, access)' not in
            (root/'src/server/agent_session_http.cpp').read_text() or
            'state.ReadRecord(link.GetString("response_id"), access)' not in
            (root/'src/server/agent_subagent.cpp').read_text()):
        raise ValueError("review scoped subagent history routing")
    session = '/v1/agents/sessions'
    resource = session + '/{session_id}'
    for method, path in [('POST', session), ('GET', session), ('POST', resource),
                         ('POST', resource + '/events'),
                         ('GET', resource + '/events'),
                         ('GET', resource), ('DELETE', resource),
                         ('DELETE', resource + '/artifacts/{artifact_id}'),
                         *(('GET', resource + suffix) for suffix in
                           ('/turns', '/turns/{turn_id}', '/items', '/turns/{turn_id}/items', '/traces',
                            '/artifacts', '/artifacts/{artifact_id}', '/artifacts/{artifact_id}/content',
                            '/subagents', '/subagents/{subagent_id}', '/subagents/{subagent_id}/items',
                            '/subagents/{subagent_id}/turns', '/subagents/{subagent_id}/turns/{turn_id}',
                            '/subagents/{subagent_id}/turns/{turn_id}/items'))]:
        routes.append((path, method))
        operations = method != 'POST' and path != resource + '/events' and not path.endswith('/content')
        native_routes.append((method, path, 'true' if operations else 'false'))
    if 'IsFileTarget(path)' not in dispatch or \
            'file_request->Serve(fd)' not in dispatch or \
            'inline bool FileMethodAllowed' not in application_routes:
        raise ValueError("review native Files transport extractor")
    files = [("POST", "/v1/files"), ("GET", "/v1/files"),
             ("GET", "/v1/files/{file_id}"),
             ("DELETE", "/v1/files/{file_id}"),
             ("GET", "/v1/files/{file_id}/content")]
    routes.extend((path, method) for method, path in files)
    # The core is being integrated first. Only record an ingress route after
    # its actual dispatcher exists; common parsing alone is not API support.
    if 'server::FileMethodAllowed(method, path)' in native:
        native_routes.extend((method, path, "false") for method, path in files)
    if 'IsUploadTarget(path)' in dispatch:
        http = (root / 'src/server/files_http.cpp').read_text()
        if 'PrepareUpload(method, target' not in http or \
                'uploads.Complete(access_, id_, body)' not in http or \
                'uploads.Cancel(access_, id_)' not in http or \
                'part_->Finish()' not in http:
            raise ValueError('review actual Uploads handlers before cataloguing')
        uploads = [('POST', '/v1/uploads'),
                   *(('POST', '/v1/uploads/{upload_id}/' + action)
                     for action in ('parts', 'complete', 'cancel'))]
        routes.extend((path, method) for method, path in uploads)
        if 'server::UploadMethodAllowed(method, path)' in native:
            native_routes.extend((method, path, 'true' if path.endswith('/cancel')
                                  else 'false') for method, path in uploads)
    if 'IsBatchTarget(path)' in dispatch:
        batch = (root / 'src/server/batch_api.cpp').read_text()
        if not re.search(r'HandleBatchRequest\(\s*\*objects_', dispatch) or \
                'jobs->Submit(access, options, client)' not in batch or \
                'jobs->Cancel(access, id)' not in batch or \
                'inline bool BatchMethodAllowed' not in application_routes:
            raise ValueError('review actual Batch owner and handlers before cataloguing')
        batches = [('POST', '/v1/batches'), ('GET', '/v1/batches'),
                   ('GET', '/v1/batches/{batch_id}'),
                   ('POST', '/v1/batches/{batch_id}/cancel')]
        routes.extend((path, method) for method, path in batches)
        if 'server::BatchMethodAllowed(method, path)' in native:
            native_routes.extend((method, path, 'false' if path == '/v1/batches'
                                  else 'true') for method, path in batches)
    if 'IsVectorStoreTarget(path)' in dispatch:
        vector_api = (root / 'src/server/vector_store_api.cpp').read_text()
        if not re.search(r'HandleVectorStoreRequest\(\s*\*objects_', dispatch) or \
                'stores.CreateBatch(access, id, request)' not in vector_api or \
                'stores.FileContent(access, id, child)' not in vector_api or \
                'stores.CancelBatch(access, id, child)' not in vector_api or \
                'inline VectorStoreRoute ParseVectorStoreRoute' not in application_routes:
            raise ValueError('review actual vector store handlers before cataloguing')
        vector_root = '/v1/vector_stores'
        store = vector_root + '/{vector_store_id}'
        vectors = [*((method, vector_root) for method in ('GET', 'POST')),
                   *((method, store) for method in ('GET', 'POST', 'DELETE')),
                   *((method, store + '/files') for method in ('GET', 'POST')),
                   *((method, store + '/files/{file_id}')
                     for method in ('GET', 'POST', 'DELETE')),
                   ('GET', store + '/files/{file_id}/content'),
                   ('POST', store + '/file_batches'),
                   ('GET', store + '/file_batches/{batch_id}'),
                   ('GET', store + '/file_batches/{batch_id}/files'),
                   ('POST', store + '/file_batches/{batch_id}/cancel')]
        if 'HandleVectorSearch(fd, method, path, body)' in dispatch:
            search = (root / 'src/server/chat_vector_search.cpp').read_text()
            api = (root / 'src/server/vector_search_api.cpp').read_text()
            if 'HandleVectorSearchRequest(' not in search or \
                    'RunNativeEmbeddingInput(' not in search or \
                    'RunInternalModelStep(' not in search or \
                    'ExecuteVectorSearchRequest(' not in api or \
                    'Kind::kSearch' not in application_routes:
                raise ValueError('review actual native search executor before cataloguing')
            vectors.append(('POST', store + '/search'))
        routes.extend((path, method) for method, path in vectors)
        if 'server::VectorStoreMethodAllowed(method, path)' in native:
            native_routes.extend((method, path, 'true' if
                path.endswith('/cancel') or
                (method in ('GET', 'DELETE') and
                 path.endswith(('/{vector_store_id}', '/{file_id}', '/{batch_id}')))
                else 'false') for method, path in vectors)
    if 'HandleMcpTunnelRequest(*mcp_tunnels_' in dispatch:
        tunnel_api = (root / 'src/server/mcp_tunnel_api.cpp').read_text()
        if 'broker.Poll(access, route.id' not in tunnel_api or \
                'broker.Respond(access, route.id' not in tunnel_api or \
                'server::McpTunnelMethodAllowed(method, path)' not in native or \
                'inline McpTunnelApiRoute ParseMcpTunnelRoute' not in application_routes:
            raise ValueError('review actual native tunnel route handlers')
        tunnels = [('GET', '/v1/tunnels/{tunnel_id}'),
                   ('GET', '/v1/tunnels/{tunnel_id}/poll'),
                   ('POST', '/v1/tunnels/{tunnel_id}/response')]
        routes.extend((path, method) for method, path in tunnels)
        native_routes.extend((method, path, 'true' if path.endswith('{tunnel_id}')
                              else 'false') for method, path in tunnels)
    if 'if (method == "OPTIONS")' in native:
        if 'config.cors.Preflight(' not in native:
            raise ValueError("review native OPTIONS handler before cataloguing")
        native_routes.extend(("OPTIONS", path, "true")
                             for path in sorted({row[1] for row in native_routes}))
    if not fields or not routes:
        raise ValueError("empty protocol inventory")
    nested = {}
    for file in ("chat_contract.cpp", "output_constraints.cpp",
                 "input_contract.cpp", "chat_tool_history.cpp",
                 "chat_template.cpp", "legacy_tools.cpp", "completion_plan.cpp",
                 "conversation_api.cpp", "responses_plan.cpp", "responses_api.cpp",
                 "responses_tokens.cpp",
                 "chat_storage.cpp", "prompt_api.cpp", "prompt_template.cpp",
                 "moderation.cpp", "moderations_plan.cpp", "embeddings_plan.cpp", "access_program.cpp", "files.cpp", "uploads.cpp",
                 "file_input.cpp", "file_fetch_policy.cpp", "batch_input.cpp", "batch_state.cpp", "batch_api.cpp",
                 "vector_store_api.cpp", "vector_stores_internal.cpp",
                 "vector_store_batches.cpp", "vector_file_page.cpp"):
        source = (root / "src/server" / file).read_text()
        for index, item in enumerate(re.finditer(
                r'(?:Input)?Fields\(\s*([^,]+),\s*\{([^}]+)\}', source)):
            nested[f"{file}:{index}:{item[1]}"] = re.findall(
                r'"([a-z_]+)"', item[2])
    # These anchors intentionally cover more than the wire protocol identity:
    # reader, cancellation, scheduling, configuration, and tokenizer matter too.
    paths = sorted(set(root.glob("src/server/**/*.*")) |
                   set(root.glob("include/q4t/server/**/*.*")) |
                   set(root.glob("src/text/**/*.cpp")) |
                   set(root.glob("include/q4t/text/**/*.h")) |
                   set(root.glob("src/service/**/*.cpp")) |
                   set(root.glob("src/service/**/*.h")) |
                   {root / "cmake/NativeSip.cmake", root / "cmake/NativeWebRTC.cmake"} |
                   set(root.glob("include/q4t/service/**/*.h")) |
                   set(root.glob("src/audio/**/*.cpp")) |
                   set(root.glob("src/audio/**/*.cu")) |
                   set(root.glob("src/audio/**/*.h")) |
                   set(root.glob("include/q4t/audio/**/*.h")) |
                   {p for p in root.glob("tools/serve_recovery/*.py")
                    if not p.name.startswith("test_")} |
                   {p for p in root.glob("tools/serve_service/*.py")
                    if not p.name.startswith("test_")} | {
                       root / "src/io/json.cpp", root / "include/q4t/io/json.h",
                       root / "src/trace/sha256.cpp",
                       root / "include/q4t/trace/sha256.h",
                       root / "include/q4t/io/base64.h",
                       root / "src/model/model_head.cu",
                       root / "include/q4t/model/model_head.h",
                       root / "src/text/tokenizer.cpp",
                       root / "include/q4t/text/tokenizer.h",
                       root / "src/runtime/system_memory.cpp",
                       root / "src/runtime/startup_reservation.cpp",
                       root / "src/runtime/cuda_allocation_quota.cpp",
                       root / "src/runtime/cuda_quota_backend.h",
                       root / "include/q4t/runtime/cuda_allocation_quota.h",
                       root / "include/q4t/runtime/allocation_quota.h",
                       root / "include/q4t/runtime/cleanup_policy.h",
                       root / "include/q4t/runtime/device_memory.h",
                       root / "include/q4t/runtime/model_health.h",
                       root / "include/q4t/runtime/execution_progress.h",
                       root / "include/q4t/runtime/prefill_kernels.h",
                       root / "include/q4t/model/linear.h",
                       root / "src/model/full_attention.cu",
                       root / "src/model/linear_attention.cu",
                       root / "src/model/hyperconnection.cu",
                       root / "src/quant/moe_gemm.cu",
                       root / "include/q4t/ple/page_reader.h",
                       root / "include/q4t/ple/ple_embedding.h",
                       root / "src/ple/page_reader.cpp",
                       root / "src/ple/ple_embedding.cpp",
                       root / "include/q4t/vision/vision.h",
                       root / "include/q4t/vision/processor.h",
                       root / "include/q4t/vision/timing.h",
                       root / "include/q4t/vision/forward_timing.h",
                       root / "include/q4t/vision/forward_timing_core.h",
                       root / "include/q4t/vision/media_plan.h",
                       root / "include/q4t/vision/configuration.h",
                       root / "src/vision/media_plan.cpp",
                       root / "src/vision/vision.cu",
                       root / "src/vision/processor.cpp",
                       root / "include/q4t/model/vision_positions.h",
                       root / "include/q4t/model/embedding_config.h",
                       root / "src/model/embedding_config.cpp",
                       root / "include/q4t/model/embedding_layout.h",
                       root / "src/model/embedding_layout.cpp",
                       root / "include/q4t/model/embedding_model.h",
                       root / "src/model/embedding_model.cu",
                       root / "src/model/model_owner.cpp",
                       root / "src/model/model.cu", root / "src/mtp/mtp.cu",
                       root / "cmake/CudaCleanupBoundary.cmake",
                       root / "cmake/workspace_python.h.in",
                       root / "src/server/workspace_python.py",
                       root / "CMakeLists.txt",
                       root / "tools/serve_gateway/gateway.py",
                       root / "tools/serve_gateway/client_policy.py",
                       root / "tools/serve_host/native.py",
                       root / "tools/serve_soak/native_contract.py",
                       root / "tools/serve_soak/native_stage.py",
                       root / "tools/serve_soak/native_report.py",
                       root / "cmake/GuardianProcess.cmake",
                       root / "cmake/NativeIngress.cmake",
                       root / "cmake/NativeCrypto.cmake",
                       root / "cmake/NativeDocuments.cmake",
                       root / "cmake/NativeVideo.cmake",
                       root / "cmake/RecoveryProbe.cmake",
                       root / "src/service/cuda_probe.cu",
                       root / "include/q4t/runtime/system_memory.h",
                       root / "src/main.cpp", root / "cmake/StructuredOutput.cmake",
                       root / "cmake/ChatProtocolIdentity.cmake",
                       root / "cmake/automation_types.h.in",
                       root / "cmake/chatkit_types.h.in",
                       root / "src/server/contracts/openai_chatkit_types_v1.json",
                       root / "cmake/webhook_types.h.in",
                       root / "src/server/contracts/openai_webhook_types_v1.json",
                       root / "cmake/container_types.h.in", root / "cmake/skill_types.h.in",
                       root / "tools/serve_responses/generate_item_types.mjs",
                       root / "cmake/RustRegex.cmake",
                       root / "cmake/regex-syntax-0.8.8.sha256",
                       root / "src/server/regex_syntax/lib.rs"})
    return {"fields": sorted(fields), "completion_fields": sorted(completion_fields),
            "routes": sorted(f"{method} {path}" for path, method in routes),
            "native_routes": sorted(f"{method} {path}" for method, path, _ in native_routes),
            "whitelists": nested,
            "sources_sha256": {str(p.relative_to(root)):
                               hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in paths}}


def check(inventory, actual, root=ROOT):
    errors = []
    for group in ("fields", "completion_fields", "routes", "native_routes"):
        recorded = inventory.get(group, {})
        missing = set(actual[group]) - recorded.keys()
        stale = recorded.keys() - set(actual[group])
        if missing or stale:
            errors.append(f"{group}: uncatalogued={sorted(missing)} "
                          f"removed={sorted(stale)}")
        for name, row in recorded.items():
            for key in ("semantics", "boundary", "evidence"):
                if not row.get(key):
                    errors.append(f"{group}.{name}: missing {key}")
            for path in row.get("evidence", []):
                if not (root / path).is_file():
                    errors.append(f"missing evidence reference: {path}")
    for group in ("whitelists", "sources_sha256"):
        recorded = inventory[group]
        changed = sorted(k for k in recorded.keys() | actual[group].keys()
                         if recorded.get(k) != actual[group].get(k))
        if changed:
            errors.append(f"{group} requires renewed review: {changed}")
    return errors


def mutation_checks(inventory, actual):
    # Independent failure modes of the anti-omission gate, not serving tests.
    cases = [dict(actual, fields=actual["fields"] + ["uncatalogued_option"]),
             dict(actual, completion_fields=actual["completion_fields"] + ["uncatalogued_option"]),
             dict(actual, routes=actual["routes"] + ["GET /uncatalogued"]),
             dict(actual, native_routes=actual["native_routes"] + ["GET /uncatalogued"]),
             dict(actual, sources_sha256=dict(actual["sources_sha256"],
                                             **{"src/server/chat_http.cpp": "0"}))]
    for group in ("routes", "native_routes"):
        omitted = "POST /v1/audio/voice_consents"
        if omitted not in actual[group]:
            raise ValueError("Voice/Consent routes escaped extraction")
        cases.append(dict(actual, **{group: [route for route in actual[group]
                                            if route != omitted]}))
        invocation = "POST /v1/agents/automations/{automation_id}/invocations"
        if invocation not in actual[group]:
            raise ValueError("Manual invocation escaped extraction")
        cases.append(dict(actual, **{group: [r for r in actual[group] if r != invocation]}))
        session = "POST /v1/agents/sessions"
        if session not in actual[group]:
            raise ValueError("Managed session creation escaped extraction")
        cases.append(dict(actual, **{group: [route for route in actual[group] if route != session]}))
        trace = "DELETE /v1/realtime/traces/{trace_id}"
        if trace not in actual[group]:
            raise ValueError("trace deletion escaped extraction")
        cases.append(dict(actual, **{group: [route for route in actual[group]
                                            if route != trace]}))
        content = "GET /v1/live/sessions/{session_id}/content"
        if content not in actual[group]:
            raise ValueError("Live download escaped extraction")
        cases.append(dict(actual, **{group: [route for route in actual[group] if route != content]}))
        for method in ("GET", "POST"):
            fork = method + " /v1/live/sessions/{session_id}/fork"
            if fork not in actual[group]:
                raise ValueError("Live fork routing escaped extraction")
            cases.append(dict(actual, **{group: [r for r in actual[group] if r != fork]}))
        media = "POST /v1/live/sessions"
        if media not in actual[group]:
            raise ValueError("Live HTTP creation escaped extraction")
        cases.append(dict(actual, **{group: [route for route in actual[group] if route != media]}))
        live = "GET /v1/live/sessions"
        if live not in actual[group]:
            raise ValueError("Live primary route escaped extraction")
        cases.append(dict(actual, **{group: [route for route in actual[group]
                                            if route != live]}))
    for case in cases:
        if not check(inventory, case):
            raise ValueError("inventory mutation escaped detection")
    manifest = (ROOT / "cmake/ChatProtocolIdentity.cmake").read_text()
    for path in ("src/server/responses_tool_search_execution.cpp",
                 "include/q4t/server/responses_tool_search.h"):
        missing = missing_protocol_sources(ROOT, manifest.replace(path, ""))
        if path not in missing:
            raise ValueError("protocol source omission escaped detection: " + path)
    private_headers = sorted(ROOT.glob("src/audio/**/*.h"))
    for header in private_headers:
        name = str(header.relative_to(ROOT))
        if name not in actual["sources_sha256"]:
            raise ValueError("audio private source omitted: " + name)
        changed = dict(actual["sources_sha256"])
        changed.pop(name)
        if not check(inventory, dict(actual, sources_sha256=changed)):
            raise ValueError("audio private source omission escaped: " + name)
    text_paths = ("src/text/voice_consent.cpp", "include/q4t/text/voice_consent.h")
    for name in text_paths:
        if name not in actual["sources_sha256"]:
            raise ValueError("voice consent text source omitted: " + name)
        missing = missing_protocol_sources(ROOT, manifest.replace(name, ""))
        if name not in missing:
            raise ValueError("text protocol omission escaped: " + name)
        changed = dict(actual["sources_sha256"])
        changed.pop(name)
        if not check(inventory, dict(actual, sources_sha256=changed)):
            raise ValueError("text source omission escaped: " + name)
    generation = (ROOT / 'src/server/chat_generation.cpp').read_text()
    native_token_observer(generation)
    for broken in (generation.replace('!stream && !native &&', '!stream &&'),
                   generation.replace('native->Emit(delta)', 'Status()')):
        try:
            native_token_observer(broken)
        except ValueError:
            pass
        else:
            raise ValueError('native output omission escaped review')
    realtime = (ROOT / 'src/server/chat_realtime.cpp').read_text()
    live = (ROOT / 'src/server/chat_live.cpp').read_text()
    for marker in ('RunRealtimeConversation(client, job, control, delta, speech)',
                   'clients_.OwnsIdentity(client)', 'tok_->Encode(',
                   'return session.Finalized()'):
        try:
            native_live_binding(realtime, live.replace(marker, 'omitted'))
        except ValueError:
            pass
        else:
            raise ValueError('native Live binding omission escaped review')
    return len(cases) + 8 + len(private_headers) + len(text_paths)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    inventory = json.loads(INVENTORY.read_text())
    actual = current_surface(ROOT)
    errors = check(inventory, actual)
    mutations = mutation_checks(inventory, actual) if args.self_check else 0
    print(json.dumps({"inventory_current": not errors,
                      "semantics_certified": False,
                      "fields": len(actual["fields"]),
                      "completion_fields": len(actual["completion_fields"]),
                      "routes": len(actual["routes"]),
                      "native_routes": len(actual["native_routes"]),
                      "reviewed_source_anchors": len(actual["sources_sha256"]),
                      "mutation_checks": mutations, "errors": errors},
                     ensure_ascii=False, indent=2))
    raise SystemExit(bool(errors))


if __name__ == "__main__":
    main()

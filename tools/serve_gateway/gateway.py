"""Prepare an explicit TLS/API-key gateway; never install or start on prepare."""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
VERSION = '1.30.6'
ROUTES = {'/v1/models': ('GET', 'control'),
          '/v1/capabilities': ('GET', 'control'),
          '/v1/chat/completions': ('POST', 'data'),
          '/v1/completions': ('POST', 'data'),
          '/v1/moderations': ('POST', 'data'),
          '/v1/embeddings': ('POST', 'data'),
          '/v1/chat/preflight': ('POST', 'data'),
          '/v1/requests/cancel': ('POST', 'control')}
DYNAMIC_ROUTES = {'/v1/models/{model}': ('GET', 'DELETE')}
# Application identities are bounded ASCII tokens; backend owns query parsing.
# These route declarations are shared by standalone and supervised nginx modes.
EVAL_ROUTES = (
    ('/v1/evals', ('GET', 'POST')),
    ('/v1/evals/{eval_id}', ('GET', 'POST', 'DELETE')),
    ('/v1/evals/{eval_id}/runs', ('GET', 'POST')),
    ('/v1/evals/{eval_id}/runs/{run_id}', ('GET', 'POST', 'DELETE')),
    ('/v1/evals/{eval_id}/runs/{run_id}/cancel', ('POST',)),
    ('/v1/evals/{eval_id}/runs/{run_id}/report', ('GET',)),
    ('/v1/evals/{eval_id}/runs/{run_id}/output_items', ('GET',)),
    ('/v1/evals/{eval_id}/runs/{run_id}/output_items/{output_item_id}', ('GET',)),
)

APPLICATION_ROUTES = [
    ('POST', '/v1/audio/transcriptions', 'data'),
    ('POST', '/v1/audio/translations', 'data'),
    ('POST', '/v1/audio/speech', 'data'),
    ('POST', '/v1/decisions', 'data'),
    ('POST', '/v1/audio/voices', 'data'),
    ('DELETE', '/v1/audio/voices/{voice_id}', 'control'),
    ('GET', '/v1/audio/voice_consents', 'control'),
    ('POST', '/v1/audio/voice_consents', 'data'),
    *((method, '/v1/audio/voice_consents/{consent_id}', 'control')
      for method in ('GET','POST','DELETE')),
    ('GET', '/v1/realtime', 'realtime'),
    ('GET', '/v1/realtime/translations', 'realtime'),
    ('POST', '/v1/realtime/translations/client_secrets', 'data'),
    ('GET', '/v1/live/sessions', 'realtime'),
    ('GET', '/v1/live/sessions/{session_id}/attach', 'sideband'),
    ('POST', '/v1/live/sessions', 'data'),
    ('POST', '/v1/realtime/client_secrets', 'data'),
    ('POST', '/v1/realtime/transcription_sessions', 'data'),
    ('POST', '/v1/realtime/sessions', 'data'),
    ('POST', '/v1/realtime/calls', 'data'),
    ('POST', '/v1/realtime/translations/calls', 'data'),
    ('POST', '/v1/live/sessions/{session_id}/accept', 'data'),
    ('POST', '/v1/realtime/calls/{call_id}/accept', 'data'),
    ('POST', '/v1/live/sessions/{session_id}/hangup', 'control'),
    ('POST', '/v1/realtime/calls/{call_id}/hangup', 'control'),
    ('POST', '/v1/live/sessions/{session_id}/refer', 'control'),
    ('POST', '/v1/realtime/calls/{call_id}/refer', 'control'),
    ('POST', '/v1/live/sessions/{session_id}/reject', 'control'),
    ('POST', '/v1/realtime/calls/{call_id}/reject', 'control'),
    ('GET', '/v1/live/sessions/{session_id}/fork', 'realtime'),
    ('POST', '/v1/live/sessions/{session_id}/fork', 'data'),
    ('GET', '/v1/live/sessions/{session_id}/content', 'data'),
    ('GET', '/v1/realtime/traces', 'control'),
    *((method, '/v1/realtime/traces/{trace_id}', 'control') for method in ('GET','DELETE')),
    ('GET', '/v1/tunnels/{tunnel_id}', 'control'),
    ('GET', '/v1/tunnels/{tunnel_id}/poll', 'tunnel_poll'),
    ('POST', '/v1/tunnels/{tunnel_id}/response', 'tunnel_response'),
    *((method, path, 'data' if method == 'POST' or path.endswith('/content') else 'control')
      for path, methods in (
          ('/v1/skills', ('GET','POST')),
          ('/v1/skills/{skill_id}', ('GET','POST','DELETE')),
          ('/v1/skills/{skill_id}/content', ('GET',)),
          ('/v1/skills/{skill_id}/versions', ('GET','POST')),
          ('/v1/skills/{skill_id}/versions/{version}', ('GET','DELETE')),
          ('/v1/skills/{skill_id}/versions/{version}/content', ('GET',)),
          ('/v1/containers', ('GET','POST')),
          ('/v1/containers/{container_id}', ('GET','DELETE')),
          ('/v1/containers/{container_id}/files', ('GET','POST')),
          ('/v1/containers/{container_id}/files/{file_id}', ('GET','DELETE')),
          ('/v1/containers/{container_id}/files/{file_id}/content', ('GET',)))
      for method in methods),
    ('POST', '/v1/threads/runs', 'data'),
    ('POST', '/v1/threads/{thread_id}/runs/{run_id}/cancel', 'control'),
    ('POST', '/v1/threads/{thread_id}/runs/{run_id}/submit_tool_outputs', 'data'),
    *((method, path, 'data' if method == 'POST' else 'control')
      for path, methods in (
          ('/v1/threads/{thread_id}/runs', ('GET','POST')),
          ('/v1/threads/{thread_id}/runs/{run_id}', ('GET','POST')),
          ('/v1/threads/{thread_id}/runs/{run_id}/steps', ('GET',)),
          ('/v1/threads/{thread_id}/runs/{run_id}/steps/{step_id}', ('GET',)))
      for method in methods),
    ('POST', '/v1/threads', 'data'),
    *((method, path, 'data' if method == 'POST' else 'control')
      for path, methods in (
          ('/v1/threads/{thread_id}', ('GET', 'POST', 'DELETE')),
          ('/v1/threads/{thread_id}/messages', ('GET', 'POST')),
          ('/v1/threads/{thread_id}/messages/{message_id}', ('GET', 'POST', 'DELETE')))
      for method in methods),
    *((method, '/v1/assistants', 'control' if method == 'GET' else 'data')
      for method in ('GET', 'POST')),
    *((method, '/v1/assistants/{assistant_id}', 'data' if method == 'POST' else 'control')
      for method in ('GET', 'POST', 'DELETE')),
    *((method, '/v1/agents', 'control' if method == 'GET' else 'data')
      for method in ('GET', 'POST')),
    *((method, '/v1/agents/automations' + suffix,
       'data' if method == 'POST' else 'control')
      for suffix, methods in (
          ('', ('GET', 'POST')),
          ('/{automation_id}', ('GET', 'POST', 'DELETE')),
          ('/{automation_id}/pause', ('POST',)),
          ('/{automation_id}/resume', ('POST',)),
          ('/{automation_id}/invocations', ('GET', 'POST')),
          ('/{automation_id}/invocations/{invocation_id}', ('GET',)))
      for method in methods),
    *((method, '/v1/agents/{agent_id}', 'data' if method == 'POST' else 'control')
      for method in ('GET', 'POST', 'DELETE')),
    *((method, '/v1/vaults', 'data' if method == 'POST' else 'control')
      for method in ('GET', 'POST')),
    *((method, '/v1/vaults/{vault_id}', 'data' if method == 'POST' else 'control')
      for method in ('GET', 'POST', 'DELETE')),
    *((method, '/v1/vaults/{vault_id}/credentials',
       'data' if method == 'POST' else 'control') for method in ('GET', 'POST')),
    *((method, '/v1/vaults/{vault_id}/credentials/{credential_id}',
       'data' if method == 'POST' else 'control') for method in ('GET', 'POST', 'DELETE')),
    *((method, path, 'data' if method == 'POST' else 'control')
      for path in ('/v1/agents/environments', '/v1/agents/environments/{environment_id}/files')
      for method in ('GET', 'POST')),
    ('GET', '/v1/agents/environments/{environment_id}', 'control'),
    *((method, '/v1/agents/environments/templates', 'data' if method == 'POST' else 'control')
      for method in ('GET', 'POST')),
    *((method, '/v1/agents/environments/templates/{environment_template_id}',
       'data' if method == 'POST' else 'control') for method in ('GET', 'POST', 'DELETE')),
    ('POST', '/v1/agents/sessions', 'data'),
    ('GET', '/v1/agents/sessions', 'control'),
    ('POST', '/v1/agents/sessions/{session_id}', 'data'),
    ('POST', '/v1/agents/sessions/{session_id}/events', 'events'),
    ('GET', '/v1/agents/sessions/{session_id}/events', 'subscription'),
    *((method, '/v1/agents/sessions/{session_id}', 'control')
      for method in ('GET', 'DELETE')),
    *(('GET', '/v1/agents/sessions/{session_id}/' + suffix, 'control')
      for suffix in ('turns', 'items', 'traces', 'turns/{turn_id}',
                     'turns/{turn_id}/items', 'artifacts', 'artifacts/{artifact_id}',
                     'subagents', 'subagents/{subagent_id}', 'subagents/{subagent_id}/items',
                     'subagents/{subagent_id}/turns', 'subagents/{subagent_id}/turns/{turn_id}',
                     'subagents/{subagent_id}/turns/{turn_id}/items')),
    ('DELETE', '/v1/agents/sessions/{session_id}/artifacts/{artifact_id}', 'control'),
    ('GET', '/v1/agents/sessions/{session_id}/artifacts/{artifact_id}/content', 'data'),
    *((method, '/v1/prompts', 'control' if method == 'GET' else 'data')
      for method in ('GET', 'POST')),
    *((method, '/v1/prompts/{prompt_id}',
       'data' if method == 'POST' else 'control')
      for method in ('GET', 'POST', 'DELETE')),
    ('POST', '/v1/prompts/{prompt_id}/versions', 'data'),
    ('GET', '/v1/prompts/{prompt_id}/versions/{version}', 'control'),
    ('POST', '/v1/chatkit', 'data'),
    ('POST', '/v1/chatkit/attachments', 'data'),
    ('PUT', '/v1/chatkit/attachments/{session_id}/{file_id}', 'data'),
    ('GET', '/v1/chatkit/attachments/{session_id}/{file_id}/preview', 'data'),
    ('POST', '/v1/chatkit/sessions', 'data'),
    ('POST', '/v1/chatkit/sessions/{session_id}/cancel', 'control'),
    ('GET', '/v1/chatkit/threads', 'control'),
    ('GET', '/v1/chatkit/threads/{thread_id}', 'control'),
    ('DELETE', '/v1/chatkit/threads/{thread_id}', 'control'),
    ('GET', '/v1/chatkit/threads/{thread_id}/items', 'control'),
    *((method, path, 'data' if method == 'POST' and path in
        ('/v1/evals', '/v1/evals/{eval_id}', '/v1/evals/{eval_id}/runs') else 'control')
      for path, methods in EVAL_ROUTES for method in methods),
    ('POST', '/v1/videos/characters', 'data'),
    ('GET', '/v1/videos/characters/{character_id}', 'control'),
    ('GET', '/v1/safety/alerts/{id}', 'control'),
    ('GET', '/v1/safety/cases/{id}', 'control'),
    ('GET', '/v1/webhook_event_types', 'control'),
    *((method, '/v1/webhook_endpoints' + suffix,
       'data' if method == 'POST' else 'control')
      for suffix, methods in (
          ('', ('GET', 'POST')),
          ('/{webhook_endpoint_id}', ('GET', 'POST', 'DELETE')),
          ('/{webhook_endpoint_id}/test', ('POST',)),
          ('/{webhook_endpoint_id}/rotate_secret', ('POST',)))
      for method in methods),
    *((method, '/v1/batches', 'data') for method in ('GET', 'POST')),
    ('GET', '/v1/batches/{batch_id}', 'control'),
    ('POST', '/v1/batches/{batch_id}/cancel', 'control'),
    *((method, '/v1/vector_stores', 'data') for method in ('GET', 'POST')),
    *((method, '/v1/vector_stores/{vector_store_id}',
       'data' if method == 'POST' else 'control')
      for method in ('GET', 'POST', 'DELETE')),
    ('POST', '/v1/vector_stores/{vector_store_id}/search', 'data'),
    *((method, '/v1/vector_stores/{vector_store_id}/files', 'data')
      for method in ('GET', 'POST')),
    *((method, '/v1/vector_stores/{vector_store_id}/files/{file_id}',
       'data' if method == 'POST' else 'control')
      for method in ('GET', 'POST', 'DELETE')),
    ('GET', '/v1/vector_stores/{vector_store_id}/files/{file_id}/content', 'data'),
    ('POST', '/v1/vector_stores/{vector_store_id}/file_batches', 'data'),
    ('GET', '/v1/vector_stores/{vector_store_id}/file_batches/{batch_id}', 'control'),
    ('GET', '/v1/vector_stores/{vector_store_id}/file_batches/{batch_id}/files', 'data'),
    ('POST', '/v1/vector_stores/{vector_store_id}/file_batches/{batch_id}/cancel', 'control'),
    ('POST', '/v1/uploads', 'data'),
    ('POST', '/v1/uploads/{upload_id}/parts', 'data'),
    ('POST', '/v1/uploads/{upload_id}/complete', 'data'),
    ('POST', '/v1/uploads/{upload_id}/cancel', 'control'),
    ('POST', '/v1/files', 'data'),
    ('GET', '/v1/files', 'data'),
    *((method, '/v1/files/{file_id}', 'control') for method in ('GET', 'DELETE')),
    ('GET', '/v1/files/{file_id}/content', 'data'),
    ('POST', '/v1/responses', 'data'),
    ('POST', '/v1/responses/compact', 'data'),
    ('POST', '/v1/responses/input_tokens', 'data'),
    *((method, '/v1/responses/{response_id}', 'control')
      for method in ('GET', 'DELETE')),
    ('POST', '/v1/responses/{response_id}/cancel', 'control'),
    ('GET', '/v1/responses/{response_id}/input_items', 'control'),
    ('POST', '/v1/conversations', 'data'),
    *((method, '/v1/conversations/{conversation_id}', 'data')
      for method in ('GET', 'POST', 'DELETE')),
    *((method, '/v1/conversations/{conversation_id}/items', 'data')
      for method in ('GET', 'POST')),
    *((method, '/v1/conversations/{conversation_id}/items/{item_id}', 'data')
      for method in ('GET', 'DELETE')),
    ('GET', '/v1/chat/completions', 'control'),
    *((method, '/v1/chat/completions/{completion_id}', 'control')
      for method in ('GET', 'POST', 'DELETE')),
    ('GET', '/v1/chat/completions/{completion_id}/messages', 'control')]
API_CONTRACT = ROOT/'docs/serve-openapi-v1.json'
API_CONTRACT_BYTES = 2*1024**2
DEFAULTS = dict(per_client_inflight=2, global_inflight=8,
                per_client_rps=5, burst=10, body_bytes=16*1024**2,
                upstream_idle_seconds=1300)
CLIENT_NETWORKS = tuple(ipaddress.IPv4Network(net) for net in
    ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '127.0.0.0/8'))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_file(path, maximum=131072, private=False):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode), 'input must be a regular file')
        if private:
            require(info.st_uid == os.geteuid() and info.st_mode & 0o077 == 0,
                    'secret input must be owned by this user and mode 0600')
        raw = os.read(fd, maximum+1)
        require(len(raw) <= maximum, 'input exceeds size limit')
        return raw
    finally:
        os.close(fd)


def object_pairs(pairs):
    result = {}
    for name, value in pairs:
        require(name not in result, 'duplicate configuration key')
        result[name] = value
    return result


def parse(raw):
    return json.loads(raw, object_pairs_hook=object_pairs)


def validate_routes():
    """Check the selected public surface against the runner's API contract."""
    paths = parse(read_file(API_CONTRACT, maximum=API_CONTRACT_BYTES))['paths']
    expected = {'v1_models': 'control', 'v1_capabilities': 'control',
                'v1_chat_completions': 'data', 'v1_chat_preflight': 'data',
                'v1_requests_cancel': 'control', 'v1_completions': 'data',
                'v1_moderations': 'data', 'v1_embeddings': 'data'}
    seen = {}
    for path, (method, lane) in ROUTES.items():
        operation = paths.get(path, {}).get(method.lower(), {}).get('operationId')
        require(operation in expected and expected[operation] == lane and
                operation not in seen, 'route differs from formal API contract')
        seen[operation] = path
    require(seen.keys() == expected.keys(), 'missing public API route')
    for path, methods in DYNAMIC_ROUTES.items():
        for method in methods:
            require(paths.get(path, {}).get(method.lower(), {}).get('operationId')
                    == 'v1_models_' + method.lower(),
                    'model detail route differs from formal API contract')
    for method, path, _ in APPLICATION_ROUTES:
        require(method.lower() in paths.get(path, {}),
                'application route differs from formal API contract')
    declared = [(method, path) for path, (method, _) in ROUTES.items()]
    declared += [(method, path) for path, methods in DYNAMIC_ROUTES.items()
                 for method in methods]
    declared += [(method, path) for method, path, _ in APPLICATION_ROUTES]
    # Configuration is operations-only. All other published /v1 operations
    # must also be reachable through the optional external frontend.
    public = {(method.upper(), path) for path, methods in paths.items()
              if path.startswith('/v1/') and path != '/v1/config'
              for method in methods if method in ('get', 'post', 'delete',
                                                   'put', 'patch')}
    require(len(declared) == len(set(declared)) and set(declared) == public,
            'public API route inventory differs from optional frontend')
    return sha(API_CONTRACT)


def route_map():
    """Raw URI routes, without decoding or rewriting IDs and query values."""
    rows = ['    "~^'+re.escape(method+':'+path)+'$" '+lane+';'
            for path, (method, lane) in ROUTES.items()]
    rows.extend('    "~^'+method+':/v1/models/[^?#]+$" control;'
                for method in DYNAMIC_ROUTES['/v1/models/{model}'])
    # Static POST operations are reserved names, not retrievable Response IDs.
    rows.append('    "~^(GET|DELETE):/v1/responses/(compact|input_tokens)'
                '(?:[?][^#]*)?$" "";')
    # The exact call_id query attaches an observer under a separate capacity.
    # Keep this before the general Realtime primary rule (first regex wins).
    rows.append('    "~^GET:/v1/realtime[?]call_id=rtc_[A-Za-z0-9_-]{1,124}$" sideband;')
    for method, path, lane in APPLICATION_ROUTES:
        if path == '/v1/chatkit/attachments/{session_id}/{file_id}/preview':
            rows.append(f'    "~^{CHATKIT_PREVIEW}$" {lane};')
            continue
        if path == '/v1/chatkit/attachments/{session_id}/{file_id}':
            rows.append(f'    "~^{CHATKIT_TRANSFER}$" {lane};')
            continue
        pattern = '/'.join('agent_[A-Za-z0-9_-]{1,122}' if part == '{agent_id}' else
                           'eval_[A-Za-z0-9_-]{1,123}' if part == '{eval_id}' else
                           'char_[A-Za-z0-9_-]{1,123}' if part == '{character_id}' else
                           '[A-Za-z0-9_-]+' if part == '{id}' and path.startswith('/v1/safety/') else
                           'whe_[A-Za-z0-9_-]{1,124}' if part == '{webhook_endpoint_id}' else
                           'automation_[A-Za-z0-9_-]{1,117}' if part == '{automation_id}' else
                           'invocation_[A-Za-z0-9_-]{1,117}' if part == '{invocation_id}' else
                           'evalrun_[A-Za-z0-9_-]{1,120}' if part == '{run_id}' and path.startswith('/v1/evals/') else
                           'outputitem_[A-Za-z0-9_-]{1,117}' if part == '{output_item_id}' else
                           'run_[A-Za-z0-9_-]{1,124}' if part == '{run_id}' else
                           'step_[A-Za-z0-9_-]{1,123}' if part == '{step_id}' else
                           'cthr_[A-Za-z0-9_-]{1,123}' if part == '{thread_id}' and path.startswith('/v1/chatkit/') else
                           'cksess_[A-Za-z0-9_-]{1,121}' if part == '{session_id}' and path.startswith('/v1/chatkit/') else
                           'thread_[A-Za-z0-9_-]{1,121}' if part == '{thread_id}' else
                           'msg_[A-Za-z0-9_-]{1,124}' if part == '{message_id}' else
                           'skill_[A-Za-z0-9_-]{1,122}' if part == '{skill_id}' else
                           '[A-Za-z0-9_-]{1,128}' if part == '{version}' and path.startswith('/v1/skills/') else
                           'cntr_[A-Za-z0-9_-]{1,123}' if part == '{container_id}' else
                           'cfile_[A-Za-z0-9_-]{1,122}' if part == '{file_id}' and path.startswith('/v1/containers/') else
                           'asst_[A-Za-z0-9_-]{1,123}' if part == '{assistant_id}' else
                           'agent_session_[A-Za-z0-9_-]{1,114}' if part == '{session_id}' and path.startswith('/v1/agents/sessions/') else
                           'environment_(?!template_)[A-Za-z0-9_-]{1,116}' if part == '{environment_id}' else
                           'environment_template_[A-Za-z0-9_-]{1,107}' if part == '{environment_template_id}' else
                           'cred_[A-Za-z0-9_-]{1,123}' if part == '{credential_id}' else
                           'vault_[A-Za-z0-9_-]{1,122}' if part == '{vault_id}' else
                           'artifact_[A-Za-z0-9_-]{1,119}' if part == '{artifact_id}' else
                           'subagent_[A-Za-z0-9_-]{1,119}' if part == '{subagent_id}' else
                           'turn_[A-Za-z0-9_-]{1,123}' if part == '{turn_id}' else
                           '[A-Za-z0-9_-]{1,128}' if part.startswith('{')
                           else re.escape(part) for part in path.split('/'))
        rows.append('    "~^'+method+':'+pattern+'(?:[?][^#]*)?$" '+lane+';')
    return '\n'.join(rows)


def retry_maps():
    # nginx reports upstream bytes for the main request independently of the
    # authorization subrequest. A locally generated error can follow dispatch.
    return '''  map "$request_method:$upstream_bytes_sent" $q4t_no_retry {
    default "";
    "~^(POST|PUT|PATCH|DELETE):[1-9]" false;
  }
  map $q4t_no_retry $q4t_retry_after { default 1; false ""; }'''


def websocket_maps():
    # Only the two authenticated upgrade routes can carry WebSocket headers.
    # Validate the client's upgrade tokens before creating an upstream tunnel.
    return r'''  map $q4t_lane $q4t_ws_client { default ""; realtime $q4t_client; }
  map $q4t_lane $q4t_ws_all { default ""; realtime all; }
  map $q4t_lane $q4t_side_client { default ""; sideband $q4t_client; subscription $q4t_client; }
  map $q4t_lane $q4t_side_all { default ""; sideband all; subscription all; }
  limit_conn_zone $q4t_side_client zone=side_client:1m;
  limit_conn_zone $q4t_side_all zone=side_all:1m;
  limit_conn_zone $q4t_ws_client zone=ws_client:1m;
  limit_conn_zone $q4t_ws_all zone=ws_all:1m;
  map $http_connection $q4t_ws_token {
    default 0;
    "~*(^|,)[ \t]*upgrade[ \t]*(,|$)" 1;
  }
  map "$q4t_lane:$q4t_ws_token:$http_upgrade" $q4t_ws_upgrade {
    default ""; "realtime:1:websocket" websocket; "sideband:1:websocket" websocket;
  }
  map $q4t_ws_upgrade $q4t_ws_connection { default close; websocket upgrade; }
  map $q4t_ws_upgrade $q4t_ws_key { default ""; websocket $http_sec_websocket_key; }
  map $q4t_ws_upgrade $q4t_ws_version { default ""; websocket $http_sec_websocket_version; }
  map $http_sec_websocket_protocol $q4t_ws_realtime {
    default ""; "~(^|,)[ \t]*realtime[ \t]*(,|$)" realtime;
  }
  map $http_sec_websocket_protocol $q4t_ws_live {
    default ""; "~(^|,)[ \t]*live[ \t]*(,|$)" live;
  }
  map "$q4t_ws_upgrade:$uri" $q4t_ws_protocol {
    default "";
    "websocket:/v1/realtime" $q4t_ws_realtime;
    "websocket:/v1/live/sessions" $q4t_ws_live;
    "~^websocket:/v1/live/sessions/live_[A-Za-z0-9_-]+/(attach|fork)$" $q4t_ws_live;
  }
  map $q4t_ws_upgrade $q4t_ws_origin { default ""; websocket $http_origin; }'''


def websocket_proxy_headers():
    return '''      proxy_set_header Connection $q4t_ws_connection;
      proxy_set_header Upgrade $q4t_ws_upgrade;
      proxy_set_header Sec-WebSocket-Key $q4t_ws_key;
      proxy_set_header Sec-WebSocket-Version $q4t_ws_version;
      proxy_set_header Sec-WebSocket-Protocol $q4t_ws_protocol;
      proxy_set_header Origin $q4t_ws_origin;'''


def responses_beta_maps():
    return r'''  map $http_openai_beta $q4t_multi_agent {
    default "";
    "~(^|,)[ \t]*responses_multi_agent=v1[ \t]*(,|$)" responses_multi_agent=v1;
  }
  map $http_openai_beta $q4t_assistants_beta {
    default "";
    "~(^|,)[ \t]*assistants=v2[ \t]*(,|$)" assistants=v2;
  }
  map $http_openai_beta $q4t_chatkit_beta {
    default "";
    "~(^|,)[ \t]*chatkit_beta=v1[ \t]*(,|$)" chatkit_beta=v1;
  }
  map $http_openai_beta $q4t_agent_config {
    default "";
    "~(^|,)[ \t]*agents=v1[ \t]*(,|$)" agents=v1;
  }
  map $http_openai_beta $q4t_realtime_header {
    default "";
    "~(^|,)[ \t]*realtime=v1[ \t]*(,|$)" realtime=v1;
  }
  map $http_sec_websocket_protocol $q4t_realtime_selector {
    default $q4t_realtime_header;
    "~(^|,)[ \t]*openai-beta\.realtime-v1[ \t]*(,|$)" realtime=v1;
  }
  map $uri $q4t_responses_beta {
    default "";
    /v1/realtime $q4t_realtime_selector;
    ~^/v1/chatkit/(sessions|threads)(/|$) $q4t_chatkit_beta;
    ~^/v1/responses(/|$) $q4t_multi_agent;
    ~^/v1/(assistants|threads)(/|$) $q4t_assistants_beta;
    ~^/v1/(agents|vaults)(/|$) $q4t_agent_config;
  }'''


def tunnel_maps():
    """Optional nginx only: bounded independent lanes and protected headers.

    nginx 1.30.6 joins duplicate generic header fields with comma + space.
    Visible ASCII tokens cannot contain that space, so we reject duplicates
    without forbidding a legal comma inside a single opaque token. Never
    forward arbitrary request headers or let Connection/Trailer name a token.
    """
    maps = []
    for lane, rate in [('tunnel_poll', 10), ('tunnel_response', 20)]:
        maps.append(f'''  map $q4t_lane $q4t_{lane} {{ default ""; {lane} $q4t_client; }}
  map $q4t_lane $q4t_all_{lane} {{ default ""; {lane} all; }}
  limit_conn_zone $q4t_{lane} zone={lane}:1m;
  limit_conn_zone $q4t_all_{lane} zone=all_{lane}:1m;
  limit_req_zone $q4t_{lane} zone={lane}_rate:1m rate={rate}r/s;''')
    for name, wire in [('instance', 'x_tunnel_client_instance_id'),
                       ('shard', 'x_tunnel_shard_token')]:
        maps.append(f'''  map $http_{wire} $q4t_invalid_{name} {{
    default 1;
    "" 0;
    "~^[!-~]{{1,128}}$" 0;
  }}''')
    for header in ('connection', 'trailer'):
        maps.append(f'''  map $http_{header} $q4t_invalid_tunnel_{header} {{
    default 0;
    "~*(^|,)[ \\t]*(x-tunnel-client-instance-id|x-tunnel-shard-token)[ \\t]*(,|$)" 1;
  }}''')
    return '\n'.join(maps)


def tunnel_checks():
    return '\n'.join(f'    if ($q4t_invalid_{name} = 1) {{ return 400; }}'
        for name in ('instance', 'shard', 'tunnel_connection', 'tunnel_trailer'))


def tunnel_limits():
    return '''      limit_conn tunnel_poll 1;
      limit_conn all_tunnel_poll 4;
      limit_conn tunnel_response 2;
      limit_conn all_tunnel_response 4;
      limit_req zone=tunnel_poll_rate burst=20 nodelay;
      limit_req zone=tunnel_response_rate burst=64 nodelay;'''


def realtime_issuer_maps(clients):
    # Only the digest of the authenticated issuing key goes to the model.
    rows = '\n'.join('    "~^[Bb][Ee][Aa][Rr][Ee][Rr] '+c['api_key']+'$" '+
                     hashlib.sha256(c['api_key'].encode('ascii')).hexdigest()+';'
                     for c in clients)
    return ('  map $http_authorization $q4t_issuer_digest {\n    default "";\n'+rows+
            '\n  }\n  map "$request_method:$request_uri" $q4t_realtime_issuer {\n'
            '    default "";\n    "POST:/v1/realtime/client_secrets" "Q4T-Issuer $q4t_issuer_digest";\n    "POST:/v1/realtime/translations/client_secrets" "Q4T-Issuer $q4t_issuer_digest";\n'
            '    "POST:/v1/realtime/transcription_sessions" "Q4T-Issuer $q4t_issuer_digest";\n'
            '    "POST:/v1/realtime/sessions" "Q4T-Issuer $q4t_issuer_digest";\n  }')


CHATKIT_TRANSFER = 'PUT:/v1/chatkit/attachments/cksess_[0-9a-f]{64}/file_[0-9a-f]{32}'


CHATKIT_PREVIEW = (r'GET:/v1/chatkit/attachments/cksess_[0-9a-f]{64}/file_[0-9a-f]{32}'
                   r'/preview[?]ticket=ckpv_[A-Za-z0-9._-]{64,180}')


def chatkit_maps(clients, scoped):
    # Core verifies the active issuer signature and the exact reservation
    # before it accepts bytes. Each credential reaches only its own route.
    scheme = '[Bb][Ee][Aa][Rr][Ee][Rr] '
    tables = []
    tables += ['  map "$http_authorization:$arg_ticket" $q4t_preview_authorization {',
               '    default "";', '    "~^:ckpv_[A-Za-z0-9._-]{64,180}$" "Bearer $arg_ticket";', '  }']
    for lane, prefix in [('chatkit', 'cksec_'), ('chatkit_upload', 'ckup_'),
                         ('chatkit_preview', 'ckpv_')]:
        bearer = '$q4t_preview_authorization' if lane == 'chatkit_preview' else '$http_authorization'
        owners, proofs = [], []
        if scoped:
            for entry in clients:
                owner = entry['id']
                pattern = scheme + prefix + r'q4t1\.' + owner + r'\.[1-9][0-9]{0,15}\.[0-9a-f]{64}\.[A-Za-z0-9_-]{43}'
                owners.append(f'    "~^{pattern}$" {owner};')
                digest = hashlib.sha256(entry['api_key'].encode('ascii')).hexdigest()
                proofs.append(f'    {owner} "Q4T-ChatKit {digest} {bearer}";')
        else:
            owners.append(f'    "~^{scheme}{prefix}[0-9a-f]{{64}}$" local;')
            proofs.append(f'    local {bearer};')
        tables += [f'  map {bearer} $q4t_{lane}_owner {{', '    default "";',
                   *owners, '  }', f'  map $q4t_{lane}_owner $q4t_{lane}_proof {{',
                   '    default "";', *proofs, '  }']
    for field, default, value in [('client', 'key_client', 'owner'),
                                  ('upstream_credential', 'realtime_issuer', 'proof')]:
        tables += [f'  map "$request_method:$request_uri" $q4t_{field} {{',
                   f'    default $q4t_{default};']
        if field == 'upstream_credential':
            tables.append('    "POST:/v1/chatkit/sessions" "Q4T-Issuer $q4t_issuer_digest";')
        for path in ['/v1/chatkit', '/v1/chatkit/attachments']:
            tables.append(f'    "POST:{path}" $q4t_chatkit_{value};')
        tables += [f'    "~^{CHATKIT_TRANSFER}$" $q4t_chatkit_upload_{value};',
                   f'    "~^{CHATKIT_PREVIEW}$" $q4t_chatkit_preview_{value};', '  }']
    return '\n'.join(tables)


def tunnel_headers():
    return '''      proxy_set_header X-Tunnel-Client-Instance-Id $http_x_tunnel_client_instance_id;
      proxy_set_header X-Tunnel-Shard-Token $http_x_tunnel_shard_token;'''


def client_sources(values, *, dual_stack=False):
    """Canonical CIDRs, with no implicit local/trusted proxies.

    Legacy v1/v2 keep their controlled IPv4 contract. Native v3 also accepts
    explicit IPv6 and globally routed CIDRs; non-loopback always requires TLS.
    """
    require(isinstance(values, list) and 1 <= len(values) <= 32,
            'one to 32 allowed_client_cidrs required')
    networks = []
    for value in values:
        require(isinstance(value, str), 'canonical IPv4 source CIDR required')
        try:
            network = (ipaddress.ip_network(value, strict=True) if dual_stack else
                       ipaddress.IPv4Network(value, strict=True))
        except (ValueError, TypeError):
            raise ValueError('canonical IPv4 source CIDR required') from None
        require(str(network) == value, 'canonical IPv4 source CIDR required')
        require(dual_stack or any(network.subnet_of(parent) for parent in CLIENT_NETWORKS),
                'source CIDRs must be within RFC1918 or IPv4 loopback')
        require(not any(network.version == prior.version and network.overlaps(prior)
                        for prior in networks),
                'duplicate or overlapping source CIDRs')
        networks.append(network)
    return list(values)


def cors_policy(value):
    if value is None:
        return None
    require(type(value) is dict and value.keys() == {'allowed_origins',
        'allowed_methods', 'allowed_headers', 'exposed_headers',
        'allow_credentials', 'max_age'}, 'invalid CORS fields')
    require(type(value['allow_credentials']) is bool and
            type(value['max_age']) is int and 0 <= value['max_age'] <= 86400,
            'invalid CORS credentials or cache age')
    for name in ('allowed_origins', 'allowed_methods', 'allowed_headers', 'exposed_headers'):
        items = value[name]
        require(type(items) is list and (1 if name in ('allowed_origins',
            'allowed_methods') else 0) <= len(items) <= 32 and
            all(type(item) is str and 1 <= len(item) <= 2048 for item in items) and
            len(set(items)) == len(items), 'invalid CORS list')
    for origin in value['allowed_origins']:
        if origin == '*':
            require(len(value['allowed_origins']) == 1 and
                    not value['allow_credentials'], 'wildcard credentialed CORS forbidden')
        elif origin != 'null':
            try:
                uri = urlsplit(origin)
                port = uri.port
                valid = (uri.scheme in ('http', 'https') and uri.hostname and
                    not uri.username and not uri.password and not uri.path and
                    not uri.query and not uri.fragment and
                    re.fullmatch(r'https?://[A-Za-z0-9.\-\[\]:]+', origin) and
                    (port is None or 1 <= port <= 65535))
            except ValueError:
                valid = False
            require(valid, 'exact HTTP(S) origin required')
    require(set(value['allowed_methods']) <= {'GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE'},
            'invalid CORS methods')
    for name in ('allowed_headers', 'exposed_headers'):
        for header in value[name]:
            require(re.fullmatch(r'[a-z0-9-]{1,128}', header) and
                    not header.startswith('x-q4t-') and
                    header not in ({'host', 'content-length', 'transfer-encoding',
                        'connection', 'cookie'} if name == 'allowed_headers' else {'set-cookie'}),
                    'invalid CORS header')
    return value


def http_policy(value):
    defaults = dict(keep_alive_requests=100, keep_alive_idle_seconds=5,
                    request_timeout_seconds=10, path_prefix='', proxy=None)
    require(type(value) is dict and value.keys() <= defaults.keys(),
            'invalid native HTTP policy fields')
    result = dict(defaults, **value)
    for field, maximum in [('keep_alive_requests', 1000),
                            ('keep_alive_idle_seconds', 60),
                            ('request_timeout_seconds', 120)]:
        require(type(result[field]) is int and 1 <= result[field] <= maximum,
                'invalid HTTP limit: '+field)
    prefix = result['path_prefix']
    require(type(prefix) is str and len(prefix) <= 512 and (not prefix or
        (re.fullmatch(r'(?:/[a-zA-Z0-9._~-]+)+', prefix) and
         all(part not in ('.', '..') for part in prefix[1:].split('/')))),
        'invalid native path prefix')
    proxy = result['proxy']
    if proxy is not None:
        require(type(proxy) is dict and proxy.keys() ==
                {'trusted_cidrs', 'header', 'allow_stripped_prefix'},
                'invalid trusted proxy fields')
        require(proxy['header'] in ('forwarded', 'x-forwarded') and
                type(proxy['allow_stripped_prefix']) is bool and
                (not proxy['allow_stripped_prefix'] or bool(prefix)),
                'invalid trusted proxy protocol/prefix')
        result['proxy'] = dict(proxy, trusted_cidrs=client_sources(
            proxy['trusted_cidrs'], dual_stack=True))
    return result


def policy(value):
    require(isinstance(value, dict) and type(value.get('version')) is int and
            value['version'] in (1, 2, 3, 4, 5), 'unsupported policy version')
    required = {'version', 'listen_host', 'listen_port', 'server_name',
                'upstream_port', 'clients'}
    if value['version'] >= 2:
        required.add('allowed_client_cidrs')
    if value['version'] >= 5:
        required.add('file_body_bytes')
    require(isinstance(value, dict) and required <= value.keys() and
            value.keys() <= required | DEFAULTS.keys() |
            ({'upstream_operations_port'} if value['version'] >= 2 else set()) |
            ({'cors'} if value['version'] >= 3 else set()) |
            ({'http'} if value['version'] >= 4 else set()),
            'unknown/missing policy field')
    value = dict(DEFAULTS, **value)
    if value['version'] >= 2:
        value['allowed_client_cidrs'] = client_sources(value['allowed_client_cidrs'],
                                                       dual_stack=value['version'] >= 3)
    if value['version'] >= 3:
        value['cors'] = cors_policy(value.get('cors'))
    if value['version'] >= 4:
        value['http'] = http_policy(value.get('http', {}))
    require(isinstance(value['listen_host'], str), 'numeric IPv4 string required')
    try:
        address = (ipaddress.ip_address(value['listen_host']) if value['version'] >= 3 else
                   ipaddress.IPv4Address(value['listen_host']))
    except (ValueError, TypeError):
        raise ValueError('numeric IPv4 listen_host required') from None
    private = address.version == 4 and any(address in ipaddress.IPv4Network(net) for net in
                  ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
    require((value['version'] >= 3 and not address.is_unspecified and
             not address.is_multicast and '%' not in value['listen_host'] and
             str(address) == value['listen_host']) or str(address) == '127.0.0.1' or private,
            'only explicit loopback or RFC1918 interface is supported')
    name = value['server_name']
    literal_v6 = False
    if value['version'] >= 3 and isinstance(name, str):
        try:
            literal_v6 = str(ipaddress.IPv6Address(name)) == name and '%' not in name
        except ValueError:
            pass
    require(literal_v6 or (isinstance(value['server_name'], str) and re.fullmatch(
        r'(?=.{1,253}$)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', value['server_name']) and
        all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part)
            for part in value['server_name'].split('.'))), 'invalid DNS server_name')
    bounds = dict(listen_port=(1024,65535), upstream_port=(1024,65535),
        per_client_inflight=(1,8), global_inflight=(1,8), per_client_rps=(1,100),
        burst=(1,128), body_bytes=(1024,96*1024**2), upstream_idle_seconds=(10,3600))
    if value['version'] >= 5:
        bounds['file_body_bytes'] = (1024, 512 * 1024**2 + 65536)
    for name, (lower, upper) in bounds.items():
        require(type(value[name]) is int and lower <= value[name] <= upper,
                'invalid numeric policy: '+name)
    require(value['listen_port'] != value['upstream_port'] and
            value['per_client_inflight'] <= value['global_inflight'], 'conflicting limits/ports')
    if 'upstream_operations_port' in value:
        port = value['upstream_operations_port']
        require(type(port) is int and 1024 <= port <= 65535 and
                port not in (value['listen_port'], value['upstream_port']),
                'invalid operations upstream port')
    require(isinstance(value['clients'], list) and 1 <= len(value['clients']) <= 64,
            'one to 64 clients required')
    ids, keys = set(), set()
    for client in value['clients']:
        require(isinstance(client, dict) and client.keys() == {'id','api_key'},
                'invalid client fields')
        name, key = client['id'], client['api_key']
        require(isinstance(name,str) and re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', name),
                'invalid client id')
        require(isinstance(key,str) and re.fullmatch(r'[A-Za-z0-9_-]{32,128}', key),
                'API key must be 32..128 URL-safe characters')
        require(name not in ids and key not in keys, 'duplicate client/key')
        ids.add(name); keys.add(key)
    return value


def quoted(path):
    value = str(Path(path).absolute())
    require(re.fullmatch(r'[A-Za-z0-9_./-]+', value), 'unsupported path characters')
    return '"'+value+'"'


def render(value, directory, backend_policy=None):
    p = policy(value)
    require(p['version'] in (1, 2), 'legacy nginx renderer requires policy v1/v2')
    require(p['version'] == 2 or p['listen_host'] == '127.0.0.1',
            'new LAN gateways require policy v2 with allowed_client_cidrs')
    source_map = source_check = source_log = ''
    upstream, upstream_map = str(p['upstream_port']), ''
    if 'upstream_operations_port' in p:
        upstream = '$q4t_upstream_port'
        upstream_map = ('  map $q4t_lane $q4t_upstream_port {\n'
                        f"    default {p['upstream_port']};\n"
                        f"    control {p['upstream_operations_port']};\n  }}\n")
    if p['version'] == 2:
        entries = '\n'.join('    '+net+' 1;' for net in p['allowed_client_cidrs'])
        # Only the socket peer. No geo proxy, realip, or PROXY protocol trust.
        source_map = ('  geo $remote_addr $q4t_source_allowed {\n'
                      '    default 0;\n'+entries+'\n  }\n')
        source_check = '    if ($q4t_source_allowed = 0) { return 403; }\n'
        source_log = ',"source_allowed":$q4t_source_allowed'
    identity_headers = ''
    if backend_policy is not None:
        from tools.serve_gateway import client_policy as cp
        cp.match_clients(p, backend_policy)
        identity_headers = (
            'proxy_set_header X-Q4T-Client-Id $q4t_client;\n'
            '      proxy_set_header X-Q4T-Gateway-Token ' +
            backend_policy['gateway_token'] + ';')
    directory = Path(directory).absolute()
    q = lambda name: quoted(directory/name)
    # nginx literal map keys are case-insensitive. Credentials MUST instead use
    # anchored case-sensitive regexes. Tokens are restricted to regex literals.
    clients = '\n'.join('    "~^[Bb][Ee][Aa][Rr][Ee][Rr] '+c['api_key']+'$" '+c['id']+';'
                        for c in p['clients'])
    routes = route_map()
    # URI/method comparisons use regexes to reject normalization aliases and
    # queries before proxying. Never send Authorization, cookies or forwarded
    # identity headers to the unauthenticated local model listener.
    locations = []
    errors = {400: 'gateway_bad_request', 401: 'gateway_unauthorized',
        404: 'gateway_route_not_found', 408: 'gateway_request_timeout',
        413: 'gateway_body_too_large', 414: 'gateway_uri_too_long',
        429: 'gateway_rate_limit', 500: 'gateway_internal_error',
        502: 'gateway_upstream_unavailable', 503: 'gateway_unavailable',
        504: 'gateway_upstream_timeout'}
    if p['version'] == 2:
        errors[403] = 'gateway_source_denied'
    for status, code in errors.items():
        auth = 'add_header WWW-Authenticate \'Bearer realm="q4t"\' always;' if status == 401 else ''
        retry = 'add_header Retry-After $q4t_retry_after always;' if status == 429 else ''
        locations.append(f'''    error_page {status} = @error_{status};
    location @error_{status} {{
      internal;
      default_type application/json;
      add_header X-Request-Id $request_id always;
      add_header X-Should-Retry $q4t_no_retry always;
      add_header X-Q4T-Gateway-Request-Id $request_id always;
      add_header Cache-Control no-store always;
      {auth} {retry}
      return {status} '{{"error":{{"message":"Gateway rejected request","type":"gateway_error","param":null,"code":"{code}"}}}}';
    }}''')
    return f'''# Generated private configuration. Do not publish nginx -T output.
daemon off;
master_process on;
worker_processes 1;
worker_shutdown_timeout 30s;
pid {q('runtime/nginx.pid')};
error_log stderr crit;
events {{ worker_connections 256; }}
http {{
  server_tokens off;
  default_type application/json;
  client_header_buffer_size 4k;
  large_client_header_buffers 4 16k;
  client_header_timeout 10s;
  client_body_timeout 15s;
  send_timeout 15s;
  keepalive_timeout 15s;
  keepalive_requests 100;
  reset_timedout_connection on;
  client_max_body_size {p['body_bytes']};
  client_body_buffer_size 64k;
  client_body_temp_path {q('runtime/body')} 1 2;
  proxy_temp_path {q('runtime/proxy')} 1 2;
  fastcgi_temp_path {q('runtime/fastcgi')};
  uwsgi_temp_path {q('runtime/uwsgi')};
  scgi_temp_path {q('runtime/scgi')};
  log_format q4t escape=json '{{"gateway_id":"$request_id","client":"$q4t_client","lane":"$q4t_lane","status":$status,"bytes":$body_bytes_sent,"seconds":$request_time,"upstream_status":"$upstream_status","connection_limit":"$limit_conn_status","rate_limit":"$limit_req_status"{source_log}}}';
  access_log /dev/stdout q4t;
{realtime_issuer_maps(p['clients'])}
{chatkit_maps(p['clients'], backend_policy is not None)}
{source_map}{upstream_map}  map $http_authorization $q4t_key_client {{
    default "";
{clients}
  }}
  map "$request_method:$request_uri" $q4t_lane {{
    default "";
{routes}
  }}
  map $q4t_lane $q4t_data {{ default ""; data $q4t_client; }}
  map $q4t_lane $q4t_control {{ default ""; control $q4t_client; events $q4t_client; }}
  map $q4t_lane $q4t_all_data {{ default ""; data all; }}
  map $q4t_lane $q4t_all_control {{ default ""; control all; events all; }}
  limit_conn_zone $q4t_data zone=data:1m;
  limit_conn_zone $q4t_control zone=control:1m;
  limit_conn_zone $q4t_all_data zone=all_data:1m;
  limit_conn_zone $q4t_all_control zone=all_control:1m;
  limit_req_zone $q4t_data zone=data_rate:1m rate={p['per_client_rps']}r/s;
  limit_req_zone $q4t_control zone=control_rate:1m rate=10r/s;
{retry_maps()}
{websocket_maps()}
{responses_beta_maps()}
{tunnel_maps()}
  server {{
    listen {p['listen_host']}:{p['listen_port']} ssl default_server;
    server_name {p['server_name']};
    ssl_certificate {q('certificate.pem')};
    ssl_certificate_key {q('private-key.pem')};
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ECDHE+AESGCM:ECDHE+CHACHA20;
    ssl_session_tickets off;
    ssl_session_cache shared:TLS:1m;
    ssl_early_data off;
    ssl_buffer_size 4k;
{source_check}    if ($host != {p['server_name']}) {{ return 400; }}
    if ($ssl_server_name != {p['server_name']}) {{ return 400; }}
    if ($q4t_client = "") {{ return 401; }}
    if ($q4t_lane = "") {{ return 404; }}
{tunnel_checks()}
{chr(10).join(locations)}
    location / {{
      limit_conn side_client 2;
      limit_conn side_all 4;
      limit_conn ws_client 1;
      limit_conn ws_all 4;
      limit_conn data {p['per_client_inflight']};
      limit_conn all_data {p['global_inflight']};
      limit_conn control 4;
      limit_conn all_control 8;
{tunnel_limits()}
      limit_conn_status 429;
      limit_req zone=data_rate burst={p['burst']} nodelay;
      limit_req zone=control_rate burst=20 nodelay;
      limit_req_status 429;
      proxy_pass http://127.0.0.1:{upstream};
      proxy_http_version 1.1;
      proxy_pass_request_headers off;
      proxy_set_header Host 127.0.0.1;
{websocket_proxy_headers()}
      proxy_set_header Content-Type $http_content_type;
      proxy_set_header Accept $http_accept;
      proxy_set_header OpenAI-Beta $q4t_responses_beta;
      proxy_set_header Idempotency-Key $http_idempotency_key;
      proxy_set_header X-Request-Id $http_x_request_id;
{tunnel_headers()}
      proxy_set_header Authorization $q4t_upstream_credential;
      proxy_set_header X-Forwarded-Host $http_host;
      proxy_set_header X-Forwarded-Proto $scheme;
      {identity_headers}
      proxy_buffering off;
      proxy_request_buffering on;
      proxy_cache off;
      proxy_max_temp_file_size 0;
      proxy_ignore_client_abort off;
      proxy_intercept_errors off;
      proxy_next_upstream off;
      proxy_connect_timeout 2s;
      proxy_send_timeout 15s;
      proxy_read_timeout {p['upstream_idle_seconds']}s;
      proxy_hide_header X-Accel-Redirect;
      proxy_ignore_headers X-Accel-Redirect X-Accel-Buffering;
      add_header X-Q4T-Gateway-Request-Id $request_id always;
      add_header Cache-Control no-store always;
    }}
  }}
}}
'''


def save(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)


def certificate_check(certificate, key, hostname, *, trusted_ca=None):
    from tools.serve_gateway import tls
    return tls.inspect(certificate, key, hostname, trusted_ca=trusted_ca)


def prepare(policy_path, nginx, certificate, key, output, client_policy=None, *, trusted_ca=None):
    from tools.serve_gateway import tls
    api_sha256 = validate_routes()
    p = policy(parse(read_file(policy_path, private=True)))
    require(p['version'] == 2 or p['listen_host'] == '127.0.0.1',
            'new LAN gateways require policy v2 with allowed_client_cidrs')
    backend_raw, backend_policy = None, None
    if client_policy is not None:
        from tools.serve_gateway import client_policy as cp
        backend_raw, backend_policy = cp.read(client_policy)
        cp.match_clients(p, backend_policy)
    output = Path(output).absolute()
    require(output.parent.resolve().is_relative_to(ROOT/'.q4t-work') or
            output.parent.resolve().is_relative_to(ROOT/'build'), 'output outside work/build')
    require(not output.exists() and not output.is_symlink(), 'preserve existing gateway package')
    quoted(output)
    nginx = Path(nginx).resolve(strict=True)
    result = subprocess.run([str(nginx),'-V'], capture_output=True, text=True, timeout=10)
    require(result.returncode == 0 and result.stderr.startswith('nginx version: nginx/'+VERSION+'\n')
            and '--with-http_ssl_module' in result.stderr, 'requires reviewed nginx '+VERSION)
    cert_raw, key_raw = read_file(certificate), read_file(key, private=True)
    ca_raw = None if trusted_ca is None else read_file(trusted_ca)
    output.mkdir(mode=0o700)
    for name in ('runtime','runtime/body','runtime/proxy','runtime/fastcgi',
                 'runtime/uwsgi','runtime/scgi'):
        (output/name).mkdir(mode=0o700)
    save(output/'certificate.pem', cert_raw)
    save(output/'private-key.pem', key_raw)
    if ca_raw is not None:save(output/'trusted-ca.pem', ca_raw)
    material = certificate_check(output/'certificate.pem', output/'private-key.pem',
        p['server_name'], trusted_ca=None if ca_raw is None else output/'trusted-ca.pem')
    save(output/'nginx.conf', render(p, output, backend_policy).encode())
    if backend_raw is not None:
        save(output/'client-policy.json', backend_raw)
    # Keep syntax-test diagnostics private: nginx can quote configuration input.
    result = subprocess.run([str(nginx),'-e','stderr','-t','-p',str(output)+'/',
        '-c',str(output/'nginx.conf')], capture_output=True, timeout=10, umask=0o077)
    save(output/'syntax-private.log', result.stdout+result.stderr)
    require(result.returncode == 0, 'nginx syntax/certificate failure; private evidence retained')
    # nginx -t creates an empty pid path while checking filesystem access.
    # This newly created private package cannot contain a running generation.
    pid_path = output/'runtime/nginx.pid'
    if pid_path.exists():
        require(read_file(pid_path, private=True) == b'', 'unexpected syntax-test PID record')
        pid_path.unlink()
    public_policy = {k:v for k,v in p.items() if k != 'clients'}
    public_policy['client_ids'] = [c['id'] for c in p['clients']]
    manifest = dict(version=3, nginx=dict(path=str(nginx),sha256=sha(nginx),version=VERSION),
        policy=public_policy, files={name:sha(output/name) for name in
            ('certificate.pem','private-key.pem','nginx.conf')},
        generator_sha256=sha(__file__), api_contract_sha256=api_sha256,
        automatic_start=False, tls=material, tls_tool_sha256=sha(tls.__file__),
        upstream_host='127.0.0.1', production_deployment_accepted=False)
    if backend_raw is not None:
        manifest['client_policy'] = cp.binding(backend_raw)
        manifest['files']['client-policy.json'] = sha(output/'client-policy.json')
        manifest['client_policy_tool_sha256'] = sha(cp.__file__)
    if ca_raw is not None:manifest['files']['trusted-ca.pem'] = sha(output/'trusted-ca.pem')
    save(output/'manifest.json', (json.dumps(manifest,indent=2)+'\n').encode())
    return dict(package=str(output), manifest_sha256=sha(output/'manifest.json'),
                automatic_start=False)


def check(directory, digest):
    directory = Path(directory).resolve(strict=True)
    for path in (directory, *(directory/name for name in ('runtime','runtime/body',
                 'runtime/proxy','runtime/fastcgi','runtime/uwsgi','runtime/scgi'))):
        info = path.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and
                info.st_mode & 0o077 == 0, 'gateway directory ownership/permissions changed')
    require(sha(directory/'manifest.json') == digest, 'gateway manifest changed')
    manifest = parse(read_file(directory/'manifest.json', private=True))
    require(type(manifest['version']) is int and manifest['version'] in (1, 2, 3),
            'unknown gateway package')
    require(sha(manifest['nginx']['path']) == manifest['nginx']['sha256'], 'nginx changed')
    require(sha(__file__) == manifest['generator_sha256'], 'gateway tooling changed')
    require(validate_routes() == manifest.get('api_contract_sha256'),
            'gateway API contract changed')
    expected = {'nginx.conf', 'certificate.pem', 'private-key.pem'}
    if manifest['version'] == 2 or (manifest['version'] == 3 and 'client_policy' in manifest):
        from tools.serve_gateway import client_policy as cp
        expected.add('client-policy.json')
        raw, backend_policy = cp.read(directory/'client-policy.json')
        require(cp.binding(raw) == manifest['client_policy'], 'client policy binding changed')
        require(sha(cp.__file__) == manifest['client_policy_tool_sha256'], 'policy tooling changed')
        require({c['id'] for c in backend_policy['clients']} ==
                set(manifest['policy']['client_ids']), 'client policy IDs differ')
    else:
        require('client_policy' not in manifest, 'legacy gateway cannot scope clients')
    if manifest['version'] == 3:
        from tools.serve_gateway import tls
        require(sha(tls.__file__) == manifest['tls_tool_sha256'], 'TLS tooling changed')
        require(manifest['tls']['trust'] in ('explicit_ca', 'not_configured'), 'unknown TLS trust')
        if manifest['tls']['trust'] == 'explicit_ca':expected.add('trusted-ca.pem')
    for name, digest in manifest['files'].items():
        require(name in expected, 'unexpected package file')
        require(hashlib.sha256(read_file(directory/name,private=True)).hexdigest() == digest,
                'gateway input identity/permissions changed')
    require(set(manifest['files']) == expected, 'missing inputs')
    material = certificate_check(directory/'certificate.pem', directory/'private-key.pem',
        manifest['policy']['server_name'],
        trusted_ca=directory/'trusted-ca.pem' if 'trusted-ca.pem' in expected else None)
    if manifest['version'] == 3:require(material == manifest['tls'], 'TLS metadata/tool identity changed')
    return manifest


def check_backend(directory, gateway):
    from tools.serve_service import package as pkg, service, supervisor
    from tools.serve_ops import identity
    directory = Path(directory).resolve(strict=True)
    status = service.status(directory)
    require(status.get('controller_live') is True and status['state'] == 'READY',
            'requires a live READY managed backend')
    control = pkg.read(directory/'controller.json')
    manifest = pkg.verify(control['package'], control['manifest_sha256'])
    port = gateway['policy']['upstream_port']
    operations_port = gateway['policy'].get('upstream_operations_port', 0)
    require(manifest['settings']['port'] == port, 'backend port differs')
    require(manifest['settings'].get('operations_port', 0) == operations_port,
            'backend operations port differs')
    expected = gateway.get('client_policy')
    require(manifest.get('client_policy') == expected, 'gateway/backend client policy differs')
    backend = status['worker']['backend']
    require(supervisor.process_identity(backend['pid']) == backend, 'backend identity changed')
    observed = identity.process(backend['pid'], port)
    require(observed['listener_scope'] == 'loopback' and
            observed['executable_sha256'] == manifest['files']['q4t'],
            'backend must be the pinned binary on loopback')
    ops = identity.operations(observed, operations_port) if operations_port else None
    if expected is not None or operations_port:
        from tools.serve_gateway import client_policy as cp
        probe = (supervisor.probe(backend['pid'], port, operations_port)
                 if operations_port else supervisor.probe(backend['pid'], port))
        cp.observed_matches(probe.get('client_policy'), expected)
        if operations_port:
            require(probe['protocol'] == manifest['protocol_sha256'], 'backend protocol differs')
            require(probe['process'] == observed and probe['operations_process'] == ops,
                    'backend listeners changed during probe')
        require(supervisor.process_identity(backend['pid']) == backend,
                'backend changed during policy check')
    result = dict(backend=backend, listener_inode=observed['listener_inode'])
    if operations_port:
        result.update(operations_port=operations_port, operations_process=ops)
    return result


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action',required=True)
    p = sub.add_parser('prepare')
    for option in ('policy','nginx','certificate','key','output'):
        p.add_argument('--'+option,required=True,type=Path)
    p.add_argument('--client-policy', type=Path)
    p.add_argument('--trusted-ca', type=Path)
    for action in ('check','run'):
        p = sub.add_parser(action)
        p.add_argument('--package',required=True,type=Path)
        p.add_argument('--manifest-sha256',required=True)
        if action == 'run':
            p.add_argument('--backend-run',required=True,type=Path)
    args = parser.parse_args()
    if args.action == 'prepare':
        value = prepare(args.policy,args.nginx,args.certificate,args.key,args.output,
                        args.client_policy, trusted_ca=args.trusted_ca)
    else:
        value = check(args.package,args.manifest_sha256)
        if args.action == 'run':
            check_backend(args.backend_run, value)
            require(not (args.package/'runtime/nginx.pid').exists(), 'prior runtime exists; inspect it first')
            environment = {k:v for k,v in os.environ.items() if k not in
                           ('LD_PRELOAD','LD_LIBRARY_PATH','OPENSSL_CONF','OPENSSL_MODULES')}
            binary = value['nginx']['path']
            os.execve(binary,[binary,'-e','stderr','-p',str(args.package.resolve())+'/',
                              '-c',str(args.package.resolve()/'nginx.conf')],environment)
    print(json.dumps(value))


if __name__ == '__main__':
    main()

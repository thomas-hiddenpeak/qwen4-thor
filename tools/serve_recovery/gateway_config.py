"""Native/default or explicitly selected nginx entrypoint configuration."""
from pathlib import Path
import re

from tools.serve_gateway import gateway as g
from tools.serve_gateway import client_policy as cp
from tools.serve_recovery import ledger


ERRORS = {'service_recovering', 'service_quarantined', 'service_stopping',
          'service_quota_wait', 'service_binding_unavailable'}


def validate(value):
    native = value.get('version') in (2, 3, 4)
    executable = 'native_ingress' if native else 'nginx'
    ledger.keys(value, ('version', 'directory', executable, executable+'_sha256',
        'policy', 'backend_policy', 'binary_sha256', 'protocol_sha256', 'tls',
        *(('access',) if value.get('version') in (3, 4) else ())))
    ledger.require(type(value['version']) is int and value['version'] in (1, 2, 3, 4),
                   'invalid gateway config version')
    if value['version'] in (3, 4):
        access = value['access']
        ledger.keys(access, ('state_directory', 'service_id', 'state_identity'))
        ledger.require(type(access['state_directory']) is str and
            Path(access['state_directory']).is_absolute() and ledger.identifier(access['service_id']) and
            type(access['state_identity']) is list and len(access['state_identity']) == 2 and
            all(ledger.integer(n, minimum=1) for n in access['state_identity']),
            'invalid access state binding')
    for key in ('directory', executable):
        ledger.require(type(value[key]) is str and Path(value[key]).is_absolute(),
                       'absolute gateway path required')
    for key in (executable+'_sha256', 'binary_sha256', 'protocol_sha256'):
        ledger.require(type(value[key]) is str and ledger.HASH.fullmatch(value[key]),
                       'invalid gateway source identity')
    p = g.policy(value['policy'])
    cp.match_clients(p, value['backend_policy'])
    ledger.require(value['version'] != 4 or value['backend_policy']['version'] == 3,
                   'dynamic ingress requires a versioned model policy')
    ledger.require('upstream_operations_port' in p,
                   'recovery requires a separate operations listener')
    tls = value['tls']
    if native:
        ledger.require(p['version'] in (2, 3, 4), 'native ingress requires explicit CIDRs')
    else:
        ledger.require(p['version'] in (1, 2), 'nginx profile requires legacy policy')
    if tls is None:
        ledger.require(p['listen_host'] in ('127.0.0.1', '::1'),
                       'plaintext ingress is loopback only')
    else:
        ledger.keys(tls, ('certificate', 'key', 'ca', 'require_client_certificate',
                         *(('crl',) if 'crl' in tls else ()))
                    if native else ('certificate', 'key'))
        if native:
            ledger.require(type(tls['require_client_certificate']) is bool and
                type(tls['ca']) is str and
                tls['require_client_certificate'] == bool(tls['ca']),
                'mTLS requires an explicit client CA')
            ledger.require(type(tls.get('crl', '')) is str and
                (not tls.get('crl') or tls['require_client_certificate']),
                'client CRL requires mTLS and an explicit CA')
        for name in ('certificate', 'key', *(('ca',) if native and tls['ca'] else ()),
                     *(('crl',) if native and tls.get('crl') else ())):
            path = tls[name]
            ledger.require(type(path) is str and Path(path).is_absolute(),
                           'absolute TLS file path required')
            g.quoted(path)
        ledger.require(p['version'] >= 2 or p['listen_host'] == '127.0.0.1',
                       'LAN requires explicit source CIDRs')
    return value


def render(value, runtime, auth_port, secret):
    validate(value)
    ledger.require(value['version'] == 1, 'nginx rendering requires explicit nginx mode')
    p = g.policy(value['policy'])
    ledger.require(type(auth_port) is int and 1024 <= auth_port <= 65535 and
                   auth_port not in (p['listen_port'], p['upstream_port'],
                                     p['upstream_operations_port']) and
                   type(secret) is str and ledger.HASH.fullmatch(secret),
                   'invalid admission endpoint')
    q = lambda name: g.quoted(Path(runtime)/name)
    clients = '\n'.join('    "~^[Bb][Ee][Aa][Rr][Ee][Rr] '+c['api_key']+'$" '+
                        c['id']+';' for c in p['clients'])
    routes = g.route_map()
    source_map, source_check = '', ''
    if p['version'] == 2:
        entries = '\n'.join('    '+net+' 1;' for net in p['allowed_client_cidrs'])
        source_map = ('  geo $remote_addr $q4t_source_allowed {\n'
                      '    default 0;\n'+entries+'\n  }\n')
        source_check = '    if ($q4t_source_allowed = 0) { return 403; }\n'
    ssl, tls_config = '', ''
    if value['tls'] is not None:
        ssl = ' ssl'
        tls_config = f'''    ssl_certificate {g.quoted(value['tls']['certificate'])};
    ssl_certificate_key {g.quoted(value['tls']['key'])};
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ECDHE+AESGCM:ECDHE+CHACHA20;
    ssl_session_tickets off;
    ssl_session_cache shared:TLS:1m;
    ssl_early_data off;
    ssl_buffer_size 4k;
    if ($ssl_server_name != {p['server_name']}) {{ return 400; }}
'''
    errors = {400: 'gateway_bad_request', 401: 'gateway_unauthorized',
        403: 'gateway_source_denied', 404: 'gateway_route_not_found',
        408: 'gateway_request_timeout', 413: 'gateway_body_too_large',
        414: 'gateway_uri_too_long', 429: 'gateway_rate_limit'}
    locations = []
    for status, code in errors.items():
        auth = ('add_header WWW-Authenticate \'Bearer realm="q4t"\' always;'
                if status == 401 else '')
        retry = 'add_header Retry-After 1 always;' if status == 429 else ''
        locations.append(f'''    error_page {status} = @error_{status};
    location @error_{status} {{
      internal;
      add_header Cache-Control no-store always;
      {auth} {retry}
      return {status} '{{"error":{{"message":"Gateway rejected request","type":"gateway_error","param":null,"code":"{code}"}}}}';
    }}''')
    codes = '\n'.join('    '+code+' '+code+';' for code in sorted(ERRORS))
    return f'''# Private generated config contains credentials. Never publish nginx -T.
daemon off;
master_process on;
worker_processes 1;
worker_shutdown_timeout 5s;
pid {q('nginx.pid')};
error_log /dev/null crit;
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
  client_body_temp_path {q('body')} 1 2;
  proxy_temp_path {q('proxy')} 1 2;
  fastcgi_temp_path {q('fastcgi')};
  uwsgi_temp_path {q('uwsgi')};
  scgi_temp_path {q('scgi')};
  access_log off;
{source_map}  map $http_authorization $q4t_client {{
    default "";
{clients}
  }}
  map "$request_method:$request_uri" $q4t_lane {{
    default "";
{routes}
  }}
  map $q4t_lane $q4t_data {{ default ""; data $q4t_client; }}
  map $q4t_lane $q4t_control {{ default ""; control $q4t_client; }}
  map $q4t_lane $q4t_all_data {{ default ""; data all; }}
  map $q4t_lane $q4t_all_control {{ default ""; control all; }}
  map $q4t_auth_error $q4t_error {{
    default service_binding_unavailable;
{codes}
  }}
  limit_conn_zone $q4t_data zone=data:1m;
  limit_conn_zone $q4t_control zone=control:1m;
  limit_conn_zone $q4t_all_data zone=all_data:1m;
  limit_conn_zone $q4t_all_control zone=all_control:1m;
  limit_req_zone $q4t_data zone=data_rate:1m rate={p['per_client_rps']}r/s;
  limit_req_zone $q4t_control zone=control_rate:1m rate=10r/s;
  server {{
    listen {p['listen_host']}:{p['listen_port']}{ssl} default_server;
    server_name {p['server_name']};
{tls_config}{source_check}    if ($host != {p['server_name']}) {{ return 400; }}
    if ($q4t_client = "") {{ return 401; }}
    if ($q4t_lane = "") {{ return 404; }}
{chr(10).join(locations)}
    error_page 500 502 503 504 = @recovering;
    location @recovering {{
      internal;
      add_header Cache-Control no-store always;
      add_header Retry-After 1 always;
      return 503 '{{"error":{{"message":"Service temporarily unavailable","type":"service_error","param":null,"code":"$q4t_error"}}}}';
    }}
    location = /_q4t_authorize {{
      internal;
      proxy_pass http://127.0.0.1:{auth_port}/authorize;
      proxy_pass_request_body off;
      proxy_pass_request_headers off;
      proxy_set_header Content-Length "";
      proxy_set_header Host 127.0.0.1;
      proxy_set_header Connection close;
      proxy_set_header X-Q4T-Admission-Secret {secret};
      proxy_set_header X-Q4T-Client $q4t_client;
      proxy_set_header X-Q4T-Lane $q4t_lane;
      proxy_connect_timeout 1s;
      proxy_read_timeout 2s;
      proxy_next_upstream off;
    }}
    location / {{
      limit_conn data {p['per_client_inflight']};
      limit_conn all_data {p['global_inflight']};
      limit_conn control 4;
      limit_conn all_control 8;
      limit_conn_status 429;
      limit_req zone=data_rate burst={p['burst']} nodelay;
      limit_req zone=control_rate burst=20 nodelay;
      limit_req_status 429;
      auth_request /_q4t_authorize;
      auth_request_set $q4t_port $upstream_http_x_q4t_port;
      auth_request_set $q4t_token $upstream_http_x_q4t_token;
      auth_request_set $q4t_auth_error $upstream_http_x_q4t_error;
      proxy_pass http://127.0.0.1:$q4t_port;
      proxy_http_version 1.1;
      proxy_pass_request_headers off;
      proxy_set_header Host 127.0.0.1;
      proxy_set_header Connection close;
      proxy_set_header Content-Type $http_content_type;
      proxy_set_header X-Request-Id $http_x_request_id;
      proxy_set_header X-Q4T-Client-Id $q4t_client;
      proxy_set_header X-Q4T-Gateway-Token $q4t_token;
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

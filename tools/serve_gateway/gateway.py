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

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
VERSION = '1.30.6'
ROUTES = {'/v1/models': ('GET', 'control'),
          '/v1/capabilities': ('GET', 'control'),
          '/v1/chat/completions': ('POST', 'data'),
          '/v1/chat/preflight': ('POST', 'data'),
          '/v1/requests/cancel': ('POST', 'control')}
API_CONTRACT = ROOT/'docs/serve-openapi-v1.json'
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
    paths = parse(read_file(API_CONTRACT))['paths']
    expected = {'v1_models': 'control', 'v1_capabilities': 'control',
                'v1_chat_completions': 'data', 'v1_chat_preflight': 'data',
                'v1_requests_cancel': 'control'}
    seen = {}
    for path, (method, lane) in ROUTES.items():
        operation = paths.get(path, {}).get(method.lower(), {}).get('operationId')
        require(operation in expected and expected[operation] == lane and
                operation not in seen, 'route differs from formal API contract')
        seen[operation] = path
    require(seen.keys() == expected.keys(), 'missing public API route')
    return sha(API_CONTRACT)


def client_sources(values):
    """Explicit canonical IPv4 CIDRs, with no implicit local/trusted proxies."""
    require(isinstance(values, list) and 1 <= len(values) <= 32,
            'one to 32 allowed_client_cidrs required')
    networks = []
    for value in values:
        require(isinstance(value, str), 'canonical IPv4 source CIDR required')
        try:
            network = ipaddress.IPv4Network(value, strict=True)
        except (ValueError, TypeError):
            raise ValueError('canonical IPv4 source CIDR required') from None
        require(str(network) == value, 'canonical IPv4 source CIDR required')
        require(any(network.subnet_of(parent) for parent in CLIENT_NETWORKS),
                'source CIDRs must be within RFC1918 or IPv4 loopback')
        require(not any(network.overlaps(prior) for prior in networks),
                'duplicate or overlapping source CIDRs')
        networks.append(network)
    return list(values)


def policy(value):
    require(isinstance(value, dict) and type(value.get('version')) is int and
            value['version'] in (1, 2), 'unsupported policy version')
    required = {'version', 'listen_host', 'listen_port', 'server_name',
                'upstream_port', 'clients'}
    if value['version'] == 2:
        required.add('allowed_client_cidrs')
    require(isinstance(value, dict) and required <= value.keys() and
            value.keys() <= required | DEFAULTS.keys(), 'unknown/missing policy field')
    value = dict(DEFAULTS, **value)
    if value['version'] == 2:
        value['allowed_client_cidrs'] = client_sources(value['allowed_client_cidrs'])
    require(isinstance(value['listen_host'], str), 'numeric IPv4 string required')
    try:
        address = ipaddress.IPv4Address(value['listen_host'])
    except (ValueError, TypeError):
        raise ValueError('numeric IPv4 listen_host required') from None
    private = any(address in ipaddress.IPv4Network(net) for net in
                  ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
    require(str(address) == '127.0.0.1' or private,
            'only explicit loopback or RFC1918 interface is supported')
    require(isinstance(value['server_name'], str) and re.fullmatch(
        r'(?=.{1,253}$)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', value['server_name']) and
        all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part)
            for part in value['server_name'].split('.')), 'invalid DNS server_name')
    bounds = dict(listen_port=(1024,65535), upstream_port=(1024,65535),
        per_client_inflight=(1,8), global_inflight=(1,8), per_client_rps=(1,100),
        burst=(1,128), body_bytes=(1024,16*1024**2), upstream_idle_seconds=(10,3600))
    for name, (lower, upper) in bounds.items():
        require(type(value[name]) is int and lower <= value[name] <= upper,
                'invalid numeric policy: '+name)
    require(value['listen_port'] != value['upstream_port'] and
            value['per_client_inflight'] <= value['global_inflight'], 'conflicting limits/ports')
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
    require(p['version'] == 2 or p['listen_host'] == '127.0.0.1',
            'new LAN gateways require policy v2 with allowed_client_cidrs')
    source_map = source_check = source_log = ''
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
    routes = '\n'.join('    "~^'+re.escape(method+':'+path)+'$" '+lane+';'
                       for path,(method,lane) in ROUTES.items())
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
        retry = 'add_header Retry-After 1 always;' if status == 429 else ''
        locations.append(f'''    error_page {status} = @error_{status};
    location @error_{status} {{
      internal;
      default_type application/json;
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
  limit_conn_zone $q4t_data zone=data:1m;
  limit_conn_zone $q4t_control zone=control:1m;
  limit_conn_zone $q4t_all_data zone=all_data:1m;
  limit_conn_zone $q4t_all_control zone=all_control:1m;
  limit_req_zone $q4t_data zone=data_rate:1m rate={p['per_client_rps']}r/s;
  limit_req_zone $q4t_control zone=control_rate:1m rate=10r/s;
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
{chr(10).join(locations)}
    location / {{
      limit_conn data {p['per_client_inflight']};
      limit_conn all_data {p['global_inflight']};
      limit_conn control 4;
      limit_conn all_control 8;
      limit_conn_status 429;
      limit_req zone=data_rate burst={p['burst']} nodelay;
      limit_req zone=control_rate burst=20 nodelay;
      limit_req_status 429;
      proxy_pass http://127.0.0.1:{p['upstream_port']};
      proxy_http_version 1.1;
      proxy_pass_request_headers off;
      proxy_set_header Host 127.0.0.1;
      proxy_set_header Connection close;
      proxy_set_header Content-Type $http_content_type;
      proxy_set_header X-Request-Id $http_x_request_id;
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
    require(manifest['settings']['port'] == port, 'backend port differs')
    expected = gateway.get('client_policy')
    require(manifest.get('client_policy') == expected, 'gateway/backend client policy differs')
    backend = status['worker']['backend']
    require(supervisor.process_identity(backend['pid']) == backend, 'backend identity changed')
    observed = identity.process(backend['pid'], port)
    require(observed['listener_scope'] == 'loopback' and
            observed['executable_sha256'] == manifest['files']['q4t'],
            'backend must be the pinned binary on loopback')
    if expected is not None:
        from tools.serve_gateway import client_policy as cp
        probe = supervisor.probe(backend['pid'], port)
        cp.observed_matches(probe.get('client_policy'), expected)
        require(supervisor.process_identity(backend['pid']) == backend,
                'backend changed during policy check')
    return dict(backend=backend, listener_inode=observed['listener_inode'])


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

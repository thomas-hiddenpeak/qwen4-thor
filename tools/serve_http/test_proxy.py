"""Strict HTTP policy and real native proxy/prefix CPU integration."""
from contextlib import closing
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
import unittest

from tools.serve_gateway import gateway
from tools.serve_recovery import test_gateway_nginx as fixture
from tools.serve_recovery import test_native_access as tls_fixture
from tools.serve_http.test_keepalive import INGRESS

CHECKER = Path(os.environ.get('Q4T_HTTP_PROXY_CHECKER', 'build/q4t_ingress_proxy_tests')).resolve()


def settings(**changes):
    return {**dict(keep_alive_requests=3, keep_alive_idle_seconds=1,
                request_timeout_seconds=1, path_prefix='/llm', proxy=dict(
                    trusted_cidrs=['127.0.0.1/32', '10.0.0.0/8'],
                    header='forwarded', allow_stripped_prefix=True)), **changes}


class PolicyTests(unittest.TestCase):
    def check_native(self, value, accepted):
        result = subprocess.run([str(CHECKER), '--check-http'],
            input=json.dumps(value).encode(), capture_output=True, timeout=3)
        self.assertEqual(result.returncode, 0 if accepted else 1)

    def test_defaults_normalize_and_match_cpp(self):
        for raw in ({}, settings(), dict(settings(), proxy=None, path_prefix='')):
            value = gateway.http_policy(raw)
            self.check_native(value, True)
            self.assertEqual(gateway.http_policy(value), value)

    def test_limits_boolean_unknown_fields_and_prefix_rejected(self):
        cases = [dict(settings(), extra=1)]
        for field, maximum in [('keep_alive_requests', 1000),
                               ('keep_alive_idle_seconds', 60), ('request_timeout_seconds', 120)]:
            cases.extend(dict(settings(), **{field: value}) for value in (False, 0, maximum+1, 1.5))
        cases.extend(dict(settings(), path_prefix=value) for value in ('/', '/llm/', '/llm//x',
            '/llm/..', '/%2f', 'llm', '/llm?x', '/llm#', '/汉字'))
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(ValueError): gateway.http_policy(value)
                self.check_native(value, False)

    def test_proxy_overlap_unknown_mode_and_missing_authority_rejected(self):
        for change in [dict(trusted_cidrs=[]), dict(trusted_cidrs=['127.0.0.0/8','127.0.0.1/32']),
                       dict(header='automatic'), dict(allow_stripped_prefix=1), dict(extra=True)]:
            value = settings(); value['proxy'].update(change)
            with self.assertRaises(ValueError): gateway.http_policy(value)
            self.check_native(value, False)
        value = settings();value['path_prefix']=''
        with self.assertRaises(ValueError):gateway.http_policy(value)
        self.check_native(value, False)

    def test_dual_stack_proxy_profile_matches_cpp(self):
        value = settings();value['proxy'].update(header='x-forwarded',
            trusted_cidrs=['127.0.0.1/32','::1/128','fd00::/8'])
        self.check_native(gateway.http_policy(value),True)

    def test_legacy_policy_shapes_stay_unchanged_and_v4_is_explicit(self):
        # Read-only existing synthetic profile; normalization cannot silently
        # enable trusted forwarding for a legacy deployment.
        from tools.serve_recovery.test_gateway import config
        old = config(Path('/tmp/q4t-policy-fixture'))['policy']
        for version in (2,3):
            value=gateway.policy(dict(old,version=version))
            self.assertNotIn('http',value)
            with self.assertRaises(ValueError):gateway.policy(dict(old,version=version,http={}))
        value=gateway.policy(dict(old,version=4,http=settings()))
        self.assertEqual(value['http'],settings())
        self.check_native(value['http'],True)


class ProxyTests(fixture.NginxTests):
    def setUp(self):
        self.replace()

    def replace(self, **changes):
        if hasattr(self,'stack'):self.stack.close()
        self.stack=fixture.Stack(self.output,native_ingress=INGRESS,
            policy_overrides={**dict(version=4,http=settings(),
                allowed_client_cidrs=['127.0.0.1/32','192.0.2.0/24']),**changes})
        self.ready()

    def raw(self, path='/llm/v1/chat/completions', *, method='POST', fields='',
            body=b'{}', source='127.0.0.1', host='127.0.0.1', key='A'):
        with socket.create_connection(('127.0.0.1',self.stack.value['policy']['listen_port']),
                timeout=4,source_address=(source,0)) as sock:
            sock.sendall((f'{method} {path} HTTP/1.1\r\nHost: {host}\r\n'
                f'Authorization: Bearer {key*40}\r\nContent-Type: application/json\r\n'
                f'Content-Length: {len(body)}\r\nConnection: close\r\n'+fields+'\r\n').encode()+body)
            reply=http.client.HTTPResponse(sock);reply.begin()
            return reply.status,dict(reply.getheaders()),reply.read()

    def test_direct_prefix_preserves_query_encoded_model_and_scope(self):
        self.assertEqual(self.raw(body=json.dumps({'p':'x'*5000}).encode())[0],200)
        self.assertEqual(self.stack.forwarded[-1]['path'],'/v1/chat/completions')
        for key,client in [('A','alpha'),('B','beta')]:
            target='/v1/conversations/conv_1/items?limit=2&include%5B%5D=message.output_text.logprobs'
            self.assertEqual(self.raw('/llm'+target,method='GET',body=b'',key=key)[0],200)
            row=self.stack.application_requests[-1]
            self.assertEqual((row['path'],row['client']),(target,client))
        self.assertEqual(self.raw('/llm/v1/models/org%2Falias',method='GET',body=b'')[0],200)
        self.assertEqual(self.stack.model_requests[-1]['path'],'/v1/models/org%2Falias')

    def test_forwarded_chain_uses_trusted_boundary_and_public_host(self):
        fields=('Forwarded: for=198.51.100.9;host=forged, '
                'for=192.0.2.9;host="127.0.0.1:443";proto=https, for=10.0.0.2\r\n')
        self.assertEqual(self.raw(fields=fields,host='internal.example')[0],200)
        self.assertEqual(self.stack.forwarded[-1]['client'],'alpha')
        denied='Forwarded: for=192.0.2.9,for=198.51.100.9;host=127.0.0.1\r\n'
        self.assertEqual(self.raw(fields=denied)[0],403)
        self.assertEqual(len(self.stack.forwarded),1)

    def test_untrusted_forwarding_cannot_enter_allowed_client_network(self):
        for fields in ['Forwarded: for=192.0.2.9;host=127.0.0.1\r\n',
                       'X-Forwarded-For: 192.0.2.9\r\n',
                       'Forwarded: invalid\r\nX-Forwarded-Prefix: /llm\r\n']:
            self.assertEqual(self.raw(fields=fields,source='127.0.0.2')[0],403)
        self.assertEqual(self.stack.forwarded,[])

    def test_stripped_prefix_requires_exact_configured_authority(self):
        target='/v1/chat/completions'
        self.assertEqual(self.raw(target)[0],400)
        for prefix in ('/other','/llm/','/llm%2f'):
            self.assertEqual(self.raw(target,fields='Forwarded: for=192.0.2.9\r\n'
                                      f'X-Forwarded-Prefix: {prefix}\r\n')[0],400)
        self.assertEqual(self.raw(target,fields='Forwarded: for=192.0.2.9\r\n'
                                 'X-Forwarded-Prefix: /llm\r\n')[0],200)
        self.assertEqual(len(self.stack.forwarded),1)

    def test_bad_trusted_metadata_and_private_identity_do_not_dispatch(self):
        for fields in ['Forwarded: for=unknown\r\n','Forwarded: for=192.0.2.9;for=10.0.0.2\r\n',
                       'Forwarded: for=192.0.2.9;proto=file\r\n',
                       'Forwarded: for=192.0.2.9;host=other\r\n',
                       'Forwarded: for=192.0.2.9\r\nX-Q4T-Client-Id: beta\r\n']:
            self.assertEqual(self.raw(fields=fields)[0],400)
        self.assertEqual(self.stack.forwarded,[])

    def test_xforwarded_profile_and_wrong_host(self):
        http=settings();http['proxy']['header']='x-forwarded'
        self.replace(http=http)
        fields='X-Forwarded-For: 198.51.100.1,192.0.2.9,10.0.0.2\r\n'
        self.assertEqual(self.raw(fields=fields+'X-Forwarded-Host: 127.0.0.1\r\n'
            'X-Forwarded-Proto: https\r\n',host='internal.example')[0],200)
        self.assertEqual(self.raw(fields=fields+'X-Forwarded-Host: other\r\n')[0],400)
        self.assertEqual(len(self.stack.forwarded),1)

    def test_configured_count_idle_and_absolute_request_deadline(self):
        port=self.stack.value['policy']['listen_port']
        with closing(http.client.HTTPConnection('127.0.0.1',port,timeout=3)) as conn:
            original=None
            for number in range(3):
                conn.request('POST','/llm/v1/chat/completions',b'{}',{
                    'Authorization':'Bearer '+'A'*40,'Content-Type':'application/json'})
                reply=conn.getresponse();self.assertEqual(reply.status,200);reply.read()
                self.assertEqual(reply.getheader('Connection'),'close' if number==2 else 'keep-alive')
                if number==0:original=conn.sock
                if number==1:self.assertIs(conn.sock,original)
        with socket.create_connection(('127.0.0.1',port),timeout=3) as sock:
            start=time.monotonic();sock.sendall(b'GET /llm/v1/models HTTP/1.1\r\nHost: ')
            reply=http.client.HTTPResponse(sock);reply.begin();reply.read()
            self.assertEqual(reply.status,408);self.assertLess(time.monotonic()-start,2)
        with closing(http.client.HTTPConnection('127.0.0.1',port,timeout=3)) as conn:
            conn.request('GET','/llm/livez',headers={'Authorization':'Bearer '+'A'*40})
            reply=conn.getresponse();reply.read();start=time.monotonic()
            self.assertEqual(conn.sock.recv(1),b'');self.assertLess(time.monotonic()-start,2)

    def test_optional_real_proxy_and_direct_entry_both_work(self):
        owner=self
        class Proxy(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                # Explicit front configuration overrides client forwarding
                # headers, strips exactly one prefix and preserves API Key.
                target=self.path.removeprefix('/llm')
                with closing(http.client.HTTPConnection('127.0.0.1',
                        owner.stack.value['policy']['listen_port'],timeout=3)) as conn:
                    conn.request('GET',target,headers={'Host':'internal.example',
                        'Authorization':self.headers.get('Authorization',''),
                        'Forwarded':f'for={self.client_address[0]};host=127.0.0.1;proto=http',
                        'X-Forwarded-Prefix':'/llm'})
                    result=conn.getresponse();raw=result.read()
                    self.send_response(result.status);self.send_header('Content-Length',str(len(raw)))
                    self.end_headers();self.wfile.write(raw)
        proxy=ThreadingHTTPServer(('127.0.0.1',0),Proxy);proxy.daemon_threads=True
        worker=threading.Thread(target=proxy.serve_forever,daemon=True);worker.start()
        try:
            for key,status in [('A',200),('Z',401)]:
                with closing(http.client.HTTPConnection('127.0.0.1',proxy.server_port,timeout=4)) as conn:
                    conn.request('GET','/llm/v1/models',headers={'Authorization':'Bearer '+key*40,
                        'Forwarded':'for=198.51.100.1;host=attacker'})
                    result=conn.getresponse();result.read();self.assertEqual(result.status,status)
            self.assertEqual(self.raw('/llm/v1/models',method='GET',body=b'')[0],200)
        finally:
            proxy.shutdown();proxy.server_close();worker.join(2)
            self.assertFalse(worker.is_alive())


class TlsProxyTests(tls_fixture.AccessTests):
    def setUp(self):
        tls_fixture.INGRESS=INGRESS
        super().setUp()
        self.replace(version=4,http=settings(),server_name='localhost',
            allowed_client_cidrs=['127.0.0.1/32','192.0.2.0/24'])

    def test_native_tls_and_proxy_prefix_keep_cors_and_identity(self):
        headers={'Host':'localhost','Origin':'https://client.example',
                 'Content-Type':'application/json'}
        direct=self.request('POST','/llm/v1/chat/completions',body=b'{}',headers=headers)
        self.assertEqual(direct[0],200)
        headers.update(Host='internal.example',Forwarded='for=192.0.2.9;host=localhost;proto=https',
                       **{'X-Forwarded-Prefix':'/llm'})
        proxied=self.request('POST','/v1/chat/completions',body=b'{}',headers=headers)
        self.assertEqual(proxied[0],200)
        self.assertEqual(proxied[1]['Access-Control-Allow-Origin'],'https://client.example')
        self.assertEqual([r['client'] for r in self.stack.forwarded],['alpha','alpha'])

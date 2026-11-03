"""Bounded real nginx/TLS transport fixture, explicitly not model inference."""
from concurrent.futures import ThreadPoolExecutor
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_gateway import gateway as g
from tools.serve_gateway.test_gateway import certificate, example, VENDOR
from tools.serve_service import acceptance as a, package as pkg

BASE=ROOT/'.q4t-work/serve-gateway-20261103'
OUT=BASE/'transport'


class TLSConnection(http.client.HTTPSConnection):
    def connect(self):
        sock=socket.create_connection(('127.0.0.1',self.port),self.timeout)
        try:self.sock=self._context.wrap_socket(sock,server_hostname=self.host)
        except BaseException:sock.close();raise


class Fixture:
    def __init__(self):
        self.rows=[]
        self.entered=threading.Event()
        self.release=threading.Event()
        self.disconnected=threading.Event()
        self.mutex=threading.Lock()

    def handler(self):
        fixture=self
        class Handler(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def log_message(self,*args):pass
            def do_GET(self):self.handle_request()
            def do_POST(self):self.handle_request()
            def handle_request(self):
                length=int(self.headers.get('Content-Length','0'))
                raw=self.rfile.read(length)
                body=json.loads(raw) if raw else {}
                with fixture.mutex:
                    fixture.rows.append(dict(path=self.path,method=self.command,
                        authorization_seen='Authorization' in self.headers,
                        cookie_seen='Cookie' in self.headers,
                        forwarded_seen=any(k.lower().startswith('x-forwarded-') for k in self.headers),
                        client_id_seen='X-Q4T-Client' in self.headers,
                        request_id=self.headers.get('X-Request-Id'),body=body))
                try:
                    if body.get('hold'):
                        fixture.entered.set()
                        if not fixture.release.wait(10):raise RuntimeError('fixture hold expired')
                    if body.get('abort_test'):
                        self.send_response(200)
                        self.send_header('Content-Type','text/event-stream')
                        self.send_header('Connection','close');self.end_headers()
                        for _ in range(200):
                            self.wfile.write(b'data: {"fixture":true,"pad":"'+b'x'*32768+b'"}\n\n')
                            self.wfile.flush();time.sleep(.01)
                        return
                    if body.get('stream'):
                        self.send_response(200)
                        self.send_header('Content-Type','text/event-stream')
                        self.send_header('Connection','close');self.end_headers()
                        self.wfile.write(b'data: {"fixture":"first"}\n\n');self.wfile.flush()
                        fixture.entered.set()
                        if not fixture.release.wait(10):raise RuntimeError('fixture stream expired')
                        self.wfile.write(b'data: [DONE]\n\n');self.wfile.flush();return
                    if self.path=='/v1/requests/cancel':fixture.release.set()
                    value=json.dumps(dict(fixture=True,body=body)).encode()
                    self.send_response(202 if self.path.endswith('/cancel') else 200)
                    self.send_header('Content-Type','application/json')
                    self.send_header('Content-Length',str(len(value)))
                    self.send_header('Connection','close');self.end_headers()
                    self.wfile.write(value)
                except (BrokenPipeError,ConnectionResetError):fixture.disconnected.set()
        return Handler


class Run:
    def __init__(self):
        self.started=time.monotonic()
        self.attempts=0;self.starts=0;self.checks=[]
        self.process=None;self.log=None;self.packages=[]
        self.vendor=pkg.read(VENDOR)
        self.fixture=Fixture()
        self.server=None

    def budget(self):
        self.attempts+=1
        g.require(self.attempts<=96 and time.monotonic()-self.started<240,'transport budget')

    def start(self,policy):
        self.starts+=1;g.require(self.starts<=4,'gateway start bound')
        folder=OUT/f'gateway-{self.starts}'
        folder.mkdir(mode=0o700)
        path=folder/'policy-private.json'
        g.save(path,json.dumps(policy).encode())
        receipt=g.prepare(path,self.vendor['binary'],self.cert,self.key,folder/'package')
        self.packages.append(receipt)
        self.policy=policy;self.port=policy['listen_port']
        self.context=ssl.create_default_context(cafile=self.cert)
        self.log=(folder/'nginx-private.log').open('xb')
        # Fixture only: production CLI requires an actual pinned managed model.
        self.process=subprocess.Popen([self.vendor['binary'],'-e','stderr','-p',receipt['package']+'/',
            '-c',str(Path(receipt['package'])/'nginx.conf')],stdin=subprocess.DEVNULL,
            stdout=self.log,stderr=subprocess.STDOUT,start_new_session=True,umask=0o077)
        until=time.monotonic()+5
        while time.monotonic()<until:
            g.require(self.process.poll() is None,'nginx exited during startup')
            try:
                with socket.create_connection(('127.0.0.1',self.port),.1):return
            except ConnectionRefusedError:time.sleep(.02)
        raise RuntimeError('nginx listen deadline')

    def stop(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.send_signal(signal.SIGQUIT)
                try:self.process.wait(5)
                except subprocess.TimeoutExpired:
                    self.process.terminate();self.process.wait(5)
            g.require(self.process.returncode==0,'nginx did not stop normally')
            self.process=None
        if self.log is not None:self.log.close();self.log=None

    def begin(self,label,path='/v1/chat/completions',body=None,client=0,method='POST',headers=None):
        self.budget()
        connection=TLSConnection(self.policy['server_name'],self.port,context=self.context,timeout=10)
        selected={'Content-Type':'application/json'}
        if client is not None:selected['Authorization']='Bearer '+self.policy['clients'][client]['api_key']
        if headers:selected.update(headers)
        connection.request(method,path,json.dumps(body or {}),selected)
        response=connection.getresponse()
        return connection,response

    def request(self,label,expected=200,**kwargs):
        connection,response=self.begin(label,**kwargs)
        try:
            raw=response.read(2*1024**2+1)
            receipt=dict(label=label,status=response.status,headers=dict(response.getheaders()),
                         body=raw.decode(),expected=expected)
            pkg.save(OUT/(label+'.json'),receipt)
            g.require(response.status==expected and len(raw)<=2*1024**2,'unexpected fixture response: '+label)
            if expected>=400:
                error=json.loads(raw)['error'];g.require(error['type']=='gateway_error','non-JSON gateway error')
            self.checks.append(label)
            return receipt
        finally:connection.close()

    def scenarios(self):
        before=len(self.fixture.rows)
        self.request('unauthorized',401,client=None)
        key=self.policy['clients'][0]['api_key']
        self.request('wrong-key',401,headers={'Authorization':'Bearer '+'z'*43})
        changed=next((key[:i]+c.swapcase()+key[i+1:] for i,c in enumerate(key) if c.isalpha()),None)
        g.require(changed is not None,'fixture key has no caseable letter')
        self.request('case-changed-key',401,headers={'Authorization':'Bearer '+changed})
        self.request('key-suffix',401,headers={'Authorization':'Bearer '+key+'x'})
        self.request('key-prefix',401,headers={'Authorization':'Bearer x'+key})
        for n,path in enumerate(('/metrics','/v1/config','/healthz','/livez','/readyz',
                                 '/v1/chat/completions?x=1','//v1/chat/completions',
                                 '/v1/chat/%63ompletions','/v1/chat/../chat/completions')):
            self.request(f'blocked-route-{n}',404,path=path)
        self.request('wrong-method',404,method='GET')
        self.request('oversized',413,body={'data':'x'*4096})
        self.request('wrong-host',400,headers={'Host':'wrong.internal.test'})
        g.require(len(self.fixture.rows)==before,'rejected request reached upstream')
        self.request('valid-identity',body={'sentinel':'preserved'},headers={
            'Authorization':'bEaReR '+key,'Cookie':'private-fixture',
            'X-Forwarded-For':'1.2.3.4','X-Q4T-Client':'impersonated',
            'X-Request-Id':'gateway-fixture-id'})
        row=self.fixture.rows[-1]
        g.require(row['body']=={'sentinel':'preserved'} and row['request_id']=='gateway-fixture-id'
                  and not any(row[k] for k in ('authorization_seen','cookie_seen','forwarded_seen','client_id_seen')),
                  'credentials/identity forwarding violated')
        # Duplicate credentials must not select either authenticated client.
        self.budget()
        conn=TLSConnection(self.policy['server_name'],self.port,context=self.context,timeout=10)
        try:
            conn.putrequest('GET','/v1/models')
            conn.putheader('Authorization','Bearer '+key)
            conn.putheader('Authorization','Bearer '+self.policy['clients'][1]['api_key'])
            conn.endheaders();response=conn.getresponse();raw=response.read()
            pkg.save(OUT/'duplicate-key.json',dict(status=response.status,body=raw.decode()))
            g.require(response.status==400,'duplicate Authorization accepted')
            self.checks.append('duplicate-key')
        finally:conn.close()
        # Trusted CA verification and DNS identity are enforced by a real TLS client.
        for label,context,name in [('untrusted-cert',ssl.create_default_context(),self.policy['server_name']),
            ('certificate-name',self.context,'wrong.internal.test')]:
            self.budget();conn=TLSConnection(name,self.port,context=context,timeout=5)
            try:
                try:conn.request('GET','/v1/models')
                except ssl.SSLCertVerificationError:self.checks.append(label)
                else:raise RuntimeError('TLS trust/name failure not rejected')
            finally:conn.close()
        self.fixture.entered.clear();self.fixture.release.clear()
        conn,response=self.begin('stream',body={'stream':True})
        try:
            first=response.readline()
            g.require(first==b'data: {"fixture":"first"}\n' and not self.fixture.release.is_set(),
                      'first SSE event was buffered')
            self.fixture.release.set()
            tail=response.read()
            g.require(tail==b'\ndata: [DONE]\n\n','SSE tail changed')
            pkg.save(OUT/'stream.json',dict(first=first.decode(),tail=tail.decode(),status=response.status))
            self.checks.append('stream-unbuffered')
        finally:conn.close()
        self.fixture.entered.clear();self.fixture.release.clear()
        with ThreadPoolExecutor(max_workers=1) as pool:
            held=pool.submit(self.request,'held-client-alpha',body={'hold':True})
            g.require(self.fixture.entered.wait(5),'held request did not reach upstream')
            try:
                self.request('per-client-limit',429)
                self.request('independent-beta',client=1)
                self.request('discovery-control',path='/v1/models',method='GET')
                self.request('cancel-control',202,path='/v1/requests/cancel')
            finally:self.fixture.release.set()
            held.result()
        self.fixture.disconnected.clear()
        conn,response=self.begin('disconnect',body={'abort_test':True})
        response.readline();response.close();conn.close()
        g.require(self.fixture.disconnected.wait(5),'upstream did not observe client disconnect')
        self.checks.append('disconnect-propagation')
        self.request('post-disconnect-recovery')

    def execute(self):
        lock=a.guard.common_lock()
        lease=pkg.read(lock)
        g.require(lease['pid']==os.getppid() and lease['state']=='held' and
                  not a.lock_available(lock),'run this fixture under the GPU exclusive guard')
        OUT.mkdir(mode=0o700)
        self.cert,self.key=certificate(OUT)
        self.server=ThreadingHTTPServer(('127.0.0.1',18124),self.fixture.handler())
        thread=threading.Thread(target=self.server.serve_forever,daemon=True);thread.start()
        result=dict(status='FAILED',model_started=False,stub_backend=True)
        def expired(*_):raise RuntimeError('transport wall-clock bound')
        previous=signal.signal(signal.SIGALRM,expired);signal.alarm(240)
        try:
            p=example();p.update(per_client_inflight=1,global_inflight=4,
                per_client_rps=100,burst=128,body_bytes=2048)
            self.start(p);self.scenarios();self.stop()
            # New immutable package revokes alpha; old package remains intact.
            revoked=dict(p,clients=[p['clients'][1]],per_client_rps=1,burst=1)
            self.start(revoked)
            self.request('revoked-key',401,headers={'Authorization':'Bearer '+p['clients'][0]['api_key']})
            self.request('rate-first');self.request('rate-burst');self.request('rate-rejected',429)
            self.request('rate-control-independent',path='/v1/models',method='GET')
            # Stop upstream: a failed proxy attempt must not be retried/replayed.
            self.server.shutdown();self.server.server_close();thread.join(5);self.server=None
            self.request('upstream-unavailable',502,path='/v1/models',method='GET')
            self.stop()
            for receipt in self.packages:g.check(receipt['package'],receipt['manifest_sha256'])
            for log in OUT.glob('gateway-*/nginx-private.log'):
                raw=log.read_bytes()
                for c in p['clients']:g.require(c['api_key'].encode() not in raw,'API key leaked into logs')
            result.update(status='PASS_BOUNDED_TLS_TRANSPORT',checks=self.checks,cleanup_confirmed=True)
        except BaseException as error:
            result['error']=repr(error);raise
        finally:
            self.fixture.release.set()
            try:
                self.stop()
                if self.server is not None:
                    self.server.shutdown();self.server.server_close();thread.join(5)
            finally:
                signal.alarm(0);signal.signal(signal.SIGALRM,previous)
                result.update(http_attempts=self.attempts,nginx_starts=self.starts,
                    elapsed_seconds=time.monotonic()-self.started,upstream_requests=len(self.fixture.rows))
                pkg.save(OUT/'result.json',result)
                pkg.save(OUT/'upstream.json',self.fixture.rows)
        return result


if __name__=='__main__':print(json.dumps(Run().execute()))

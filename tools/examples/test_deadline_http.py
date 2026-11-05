"""Deterministic production urllib transport checks; no HTTP listener or GPU."""
import io
import http.client
import json
import subprocess
import unittest
from unittest.mock import patch,Mock
import urllib.request

import chat_client as client
import deadline_http as t


class Clock:
    now = 0.0

    def monotonic(self):
        return self.now


class Raw(io.RawIOBase):
    def __init__(self, peer):
        self.peer = peer

    def readable(self):
        return True

    def readinto(self, buf):
        if not self.peer.data:
            return 0
        self.peer.advance(self.peer.read_seconds)
        count = min(len(buf), self.peer.fragment, len(self.peer.data))
        buf[:count] = self.peer.data[:count]
        del self.peer.data[:count]
        return count


class Peer:
    def __init__(self, clock, data, *, step=.01, fragment=1):
        self.clock, self.data = clock, bytearray(data)
        self.read_seconds, self.fragment = step, fragment
        self.timeout = 99
        self.closed = False
        self.files, self.sent, self.timeouts = [], [], []
        self.send_seconds, self.send_fragment = 0, 65536

    def advance(self, seconds):
        if seconds > self.timeout:
            self.clock.now += self.timeout
            raise TimeoutError('simulated socket timeout')
        self.clock.now += seconds

    def settimeout(self, timeout):
        self.timeout = timeout
        self.timeouts.append(timeout)

    def makefile(self, mode, buffering=-1):
        assert mode == 'rb'
        raw = Raw(self)
        self.files.append(raw)
        return raw if buffering == 0 else io.BufferedReader(raw)

    def send(self, data):
        self.advance(self.send_seconds)
        size = min(len(data), self.send_fragment)
        self.sent.append(bytes(data[:size]))
        return size

    def sendall(self, data):
        while data:
            data = data[self.send(data):]

    def close(self):
        self.closed = True


class Context:
    def __init__(self, clock, delay=0):
        self.clock, self.delay = clock, delay

    def wrap_socket(self, sock, *, server_hostname):
        assert server_hostname == 'fixture.test'
        sock.advance(self.delay)
        return sock


class DeadlineTests(unittest.TestCase):
    def run_request(self, raw, *, timeout=.25, step=.01, fragment=1,
                    connect=0, handshake=0, send=0, resolve=0, consume=None):
        self.clock=Clock();self.peer=Peer(self.clock,raw,step=step,fragment=fragment)
        self.peer.send_seconds=send;self.peer.send_fragment=32 if send else 65536
        self.peer.connect=lambda _address:self.peer.advance(connect)
        self.api=client.ChatClient('https://fixture.test/v1',timeout=timeout)
        for handler in self.api.opener.handlers:
            if isinstance(handler,t.HTTPSHandler):handler._context=Context(self.clock,handshake)
        def addresses(*_args):
            self.clock.now+=resolve;t.remaining(timeout)
            return [(t.socket.AF_INET,('127.0.0.1',443))]
        with patch.object(t.time,'monotonic',self.clock.monotonic), \
             patch.object(t,'resolve',addresses), \
             patch.object(t.socket,'socket',lambda *_:self.peer):
            with self.api._open(urllib.request.Request(self.api.base+'/models'),10) as response:
                return (consume or (lambda response:response.read(10)))(response)

    def expired(self,*args,**kwargs):
        with self.assertRaises(TimeoutError):self.run_request(*args,**kwargs)
        self.assertLessEqual(self.clock.now,kwargs.get('timeout',.25)+1e-8)
        self.assertTrue(self.peer.closed)
        self.assertTrue(all(file.closed for file in self.peer.files))

    def test_deadline_dripping_status_and_headers(self):
        self.expired(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok')

    def test_deadline_resolution_connect_tls_share_one_budget(self):
        self.expired(b'',resolve=.1,connect=.1,handshake=.1)

    def test_deadline_partial_writes_do_not_reset(self):
        self.expired(b'',connect=.05,handshake=.05,send=.06)

    def test_deadline_interim_responses(self):
        self.expired(b'HTTP/1.1 100 Continue\r\n\r\n'*8,step=.06,fragment=26)

    def test_deadline_chunk_lines_and_trailers(self):
        for tail in (b'f'*100,b'0\r\nX-Slow: '+b'x'*100):
            with self.subTest(tail=tail[:4]):
                self.expired(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n'+tail,
                             step=.003,timeout=.4)

    def test_deadline_body_and_error_body_drips(self):
        for status in (b'200 OK',b'503 Busy'):
            with self.subTest(status=status):
                self.expired(b'HTTP/1.1 '+status+b'\r\nContent-Length: 100\r\n\r\n'+b'x'*100,
                             step=.003,timeout=.35,consume=lambda response:response.read(200))

    def test_deadline_completed_framing_closes_references(self):
        for framing,body in ((b'Content-Length: 2',b'ok'),
                             (b'Transfer-Encoding: chunked',b'2\r\nok\r\n0\r\n\r\n')):
            value=self.run_request(b'HTTP/1.1 200 OK\r\n'+framing+b'\r\nConnection: close\r\n\r\n'+body,step=.001)
            self.assertEqual(value,b'ok');self.assertTrue(self.peer.closed)
            self.assertTrue(all(file.closed for file in self.peer.files))

    def test_deadline_error_preserves_envelope(self):
        body=b'{"error":{"code":"bad_request","param":"x","message":"bad"}}'
        with self.assertRaises(client.RemoteError) as caught:
            self.run_request(b'HTTP/1.1 400 Bad Request\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body,
                             step=0)
        self.assertEqual(caught.exception.code,'bad_request');self.assertEqual(caught.exception.param,'x')
        self.assertTrue(self.peer.closed)

    def test_truncated_content_length_is_not_success(self):
        for consume in (lambda r: r.read(100),
                        lambda r: (r.readline(), r.readline())):
            with self.subTest(consume=consume):
                with self.assertRaises(http.client.IncompleteRead):
                    self.run_request(b'HTTP/1.1 200 OK\r\nContent-Length: 8\r\n\r\n{}',
                                     step=0, consume=consume)
                self.assertTrue(self.peer.closed)

    def test_chunked_missing_final_trailer_line_is_not_success(self):
        for tail in (b'', b'X-Test: yes\r\n'):
            with self.subTest(tail=tail):
                with self.assertRaises(http.client.IncompleteRead):
                    self.run_request(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n'
                                     b'2\r\n{}\r\n0\r\n'+tail, step=0)
                self.assertTrue(self.peer.closed)

    def test_close_delimited_http_body_remains_supported(self):
        self.assertEqual(self.run_request(b'HTTP/1.0 200 OK\r\n\r\nok',step=0),b'ok')

    def test_deadline_late_consumer_is_not_committed(self):
        def consume(response):
            value=response.read(10);self.clock.now+=1;return value
        with self.assertRaises(TimeoutError):
            self.run_request(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok',step=0,consume=consume)
        self.assertTrue(self.peer.closed)

    def test_deadline_dns_timeout_kills_and_reaps_own_child(self):
        child=Mock();child.poll.return_value=None
        child.communicate.side_effect=[subprocess.TimeoutExpired('resolver',.1),(b'',None)]
        with patch.object(t.subprocess,'Popen',return_value=child), \
             patch.object(t.time,'monotonic',return_value=0):
            with self.assertRaises(TimeoutError):t.resolve('fixture.test',443,.1)
        child.kill.assert_called_once();self.assertEqual(child.communicate.call_count,2)
        self.assertEqual(child.communicate.call_args.kwargs['timeout'],1)

    def test_deadline_dns_failure_reaps_without_kill(self):
        child=Mock();child.poll.return_value=1;child.returncode=1;child.communicate.return_value=(b'',None)
        with patch.object(t.subprocess,'Popen',return_value=child), \
             patch.object(t.time,'monotonic',return_value=0):
            with self.assertRaises(OSError):t.resolve('fixture.test',443,.1)
        child.kill.assert_not_called()

    def test_deadline_numeric_ip_does_not_spawn_dns(self):
        with patch.object(t.subprocess,'Popen') as spawn,patch.object(t.time,'monotonic',return_value=0):
            self.assertEqual(t.resolve('127.0.0.1',80,1),[(t.socket.AF_INET,('127.0.0.1',80))])
            self.assertEqual(t.resolve('::1',443,1),[(t.socket.AF_INET6,('::1',443,0,0))])
        spawn.assert_not_called()

    def test_deadline_secure_default_port_and_no_ambient_proxy(self):
        connection=t.Connection('fixture.test',timeout=1,deadline=1,context=Context(Clock()))
        self.assertEqual(connection.port,443)
        with patch.dict('os.environ',{'https_proxy':'http://untrusted.invalid:1234'}):
            api=client.ChatClient('https://fixture.test')
        self.assertFalse(any(isinstance(h,urllib.request.ProxyHandler) and h.proxies for h in api.opener.handlers))

    def test_deadline_invalid_connection_options(self):
        for value in (0,-1,float('nan'),float('inf'),True,'1'):
            with self.assertRaises(client.ProtocolError):client.ChatClient('http://localhost',timeout=value)
        for key in ('','x\r\nInjected: y','含空格'):
            with self.assertRaises(client.ProtocolError):client.ChatClient('http://localhost',api_key=key)
        with self.assertRaises(client.ProtocolError):client.ChatClient('http://localhost',ca_file='unused')
        with self.assertRaises(ValueError):client.ChatClient('http://localhost',connect_host='domain.invalid')

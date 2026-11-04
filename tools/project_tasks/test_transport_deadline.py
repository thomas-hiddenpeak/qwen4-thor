"""Deterministic elapsed-time tests, without listeners, sleeps, or model IO."""
import io
import unittest
from unittest.mock import patch

from tools.project_tasks import transport as t
from tools.project_tasks import engine, migration, runner, store
from tools.project_tasks import test_tasks as old


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


class Deadlines(unittest.TestCase):
    def run_request(self, raw, *, timeout=.25, step=.01, fragment=1,
                    connect=0, handshake=0, send=0, consume=None):
        self.clock = Clock()
        self.peer = Peer(self.clock, raw, step=step, fragment=fragment)
        self.peer.send_seconds = send
        self.peer.send_fragment = 32 if send else 65536
        client = object.__new__(t.Client)
        client.value = dict(base_url='https://fixture.test:443/v1',
                            connect_host='127.0.0.1', api_key='cpu-fixture')
        client.context = Context(self.clock, handshake)

        def connected(address, timeout):
            self.peer.settimeout(timeout)
            self.peer.advance(connect)
            return self.peer

        with patch.object(t.time, 'monotonic', self.clock.monotonic), \
                patch.object(t.socket, 'create_connection', connected):
            return client.request('GET', '/capabilities', None, timeout,
                consume or (lambda response, reader: reader.read(10)))

    def assert_expired(self, *args, **kwargs):
        with self.assertRaises(TimeoutError):
            self.run_request(*args, **kwargs)
        self.assertLessEqual(self.clock.now, kwargs.get('timeout', .25) + 1e-8)
        self.assertTrue(self.peer.closed)
        self.assertTrue(all(file.closed for file in self.peer.files))

    def test_dripping_status_and_headers_share_request_deadline(self):
        self.assert_expired(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok')

    def test_connect_and_tls_do_not_each_receive_a_new_budget(self):
        self.assert_expired(b'', connect=.15, handshake=.15)

    def test_partial_tls_writes_share_the_original_deadline(self):
        self.assert_expired(b'', connect=.05, handshake=.05, send=.06)

    def test_interim_responses_do_not_restart_header_deadline(self):
        self.assert_expired(b'HTTP/1.1 100 Continue\r\n\r\n' * 8,
                            step=.06, fragment=26)

    def test_chunk_size_lines_and_trailers_share_deadline(self):
        for tail in (b'f' * 100, b'0\r\nX-Slow: ' + b'x' * 100):
            with self.subTest(tail=tail[:4]):
                self.assert_expired(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: '
                    b'chunked\r\n\r\n' + tail, step=.003, timeout=.4)

    def test_complete_and_chunked_responses_close_all_io_references(self):
        for framing, body in ((b'Content-Length: 2', b'ok'),
                              (b'Transfer-Encoding: chunked', b'2\r\nok\r\n0\r\n\r\n')):
            with self.subTest(framing=framing):
                value = self.run_request(b'HTTP/1.1 200 OK\r\n' + framing +
                    b'\r\nConnection: close\r\n\r\n' + body, step=.001)
                self.assertEqual(value, b'ok')
                self.assertTrue(self.peer.closed)
                self.assertTrue(all(file.closed for file in self.peer.files))

    def test_body_drips_and_error_body_use_same_deadline(self):
        for status in (b'200 OK', b'503 Busy'):
            with self.subTest(status=status):
                self.assert_expired(b'HTTP/1.1 ' + status +
                    b'\r\nContent-Length: 100\r\n\r\n' + b'x' * 100,
                    step=.003, timeout=.35,
                    consume=lambda response, reader: reader.read(200))

    def test_buffered_response_is_not_accepted_after_expiry(self):
        def consume(response, reader):
            value = reader.read(10)
            self.clock.now += 1
            return value
        with self.assertRaises(TimeoutError):
            self.run_request(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok',
                             step=0, consume=consume)
        self.assertTrue(self.peer.closed)


class TaskState(unittest.TestCase):
    setUp = old.Tasks.setUp
    execute = old.Tasks.execute
    status = old.Tasks.status
    count = old.Tasks.count
    break_at = old.Tasks.break_at

    def test_generation_timeout_keeps_unknown_and_does_not_retry(self):
        fake = old.FakeClient()
        def stopped(request, *, sink):
            fake.calls.append(request)
            sink(b'{"partial":')
            raise TimeoutError('request wall deadline')
        fake.complete = stopped
        with self.assertRaises(TimeoutError):
            self.execute(fake)
        before = self.status()
        self.assertEqual((before['state'], before['requests'], self.count()),
                         ('REQUEST_UNKNOWN', 1, 0))
        untouched = old.FakeClient()
        self.assertEqual(self.execute(untouched), before)
        self.assertEqual((untouched.cap_calls, untouched.calls, self.count()), (0, [], 0))

    def test_preflight_timeout_never_submits_generation(self):
        fake = old.FakeClient()
        def stopped(*args, **kwargs):
            raise TimeoutError('request wall deadline')
        fake.preflight = stopped
        result = self.execute(fake)
        self.assertEqual((result['state'], result['requests'], self.count()),
                         ('PREFLIGHT_REJECTED', 0, 0))
        self.assertEqual(fake.calls, [])
        later = old.FakeClient()
        self.assertEqual(self.execute(later)['state'], 'COMPLETED')
        self.assertEqual(len(later.preflight_calls), 2)

    def test_registered_previous_source_migrates_without_tool_replay(self):
        entry = __import__('json').loads((store.ROOT / migration.REGISTRY).read_bytes())['releases'][-1]
        self.assertEqual(entry['release'], 'srv11i-3967b862')
        self.assertEqual(len(entry['runtime_sources']), 17)
        with patch.object(engine, 'sources', return_value=entry['runtime_sources']):
            self.setUp()
            self.break_at('tool_result')
        with self.assertRaises(store.ContractError) as caught:
            self.execute()
        self.assertEqual(caught.exception.code, 'runtime_changed')
        with store.open_task(self.root, self.name) as ledger:
            meta, events = ledger.meta, ledger.events()
        path = self.directory/'migration.json'
        plan = migration.prepare(self.root, self.binding, 'key', path, 'reviewed deadline repair')
        self.assertEqual(plan['source_release'], entry['release'])
        migration.apply(self.root, self.binding, 'key', path, plan['plan_sha256'])
        with store.open_task(self.root, self.name) as ledger:
            self.assertEqual((ledger.meta, ledger.events()[:-1]), (meta, events))
        client = old.FakeClient()
        final = self.execute(client)
        self.assertEqual((final['state'], final['requests'], final['migrations'], self.count()),
                         ('COMPLETED', 2, 1, 1))
        self.assertEqual(len(client.calls), 1)
        duplicate = runner.submit(self.root, self.binding, 'key', 'read current', client, stream=False)
        self.assertEqual(duplicate, dict(final, created=False))
        self.assertEqual(len(client.calls), 1)


if __name__ == '__main__':
    unittest.main()

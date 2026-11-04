"""Deterministic socket/clock boundaries; never opens a listener or GPU."""
import errno
import socket
import unittest
from unittest.mock import MagicMock,patch
from tools.serve_ops import transport as t
from tools.serve_ops.contracts import InvalidObservation


class StopTransport(unittest.TestCase):
    def setUp(self):
        self.clock=0.0;self.stop=False;self.sock=MagicMock()
        self.sock.__enter__.return_value=self.sock
        self.sock.connect_ex.return_value=0
        self.sock.send.side_effect=lambda data:len(data)
        self.sock.recv.side_effect=[b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Type: application/json\r\n\r\n{}',b'']
        self.patches=[patch.object(t.time,'monotonic',side_effect=lambda:self.clock),
            patch.object(t.socket,'socket',return_value=self.sock),
            patch.object(t.select,'select',side_effect=lambda r,w,x,timeout:(r,w,x))]
        for item in self.patches:item.start();self.addCleanup(item.stop)

    def get(self,**kwargs):
        return t.get(18109,'/livez',stopped=lambda:self.stop,**kwargs)

    def test_success_and_single_owned_socket(self):
        self.assertEqual(self.get()['body'],'{}')
        self.sock.connect_ex.assert_called_once_with(('127.0.0.1',18109))
        self.sock.__exit__.assert_called_once()

    def test_already_stopped_opens_nothing(self):
        self.stop=True
        with self.assertRaisesRegex(InvalidObservation,'observation_stopped'):self.get()
        self.sock.connect_ex.assert_not_called()

    def test_stop_during_connect_closes_unpublished_socket(self):
        self.sock.connect_ex.return_value=errno.EINPROGRESS
        def wait(r,w,x,timeout):self.stop=True;return [],[],[]
        with patch.object(t.select,'select',side_effect=wait):
            with self.assertRaisesRegex(InvalidObservation,'observation_stopped'):self.get()
        self.sock.close.assert_called_once();self.sock.send.assert_not_called()

    def test_async_connect_error_does_not_send(self):
        self.sock.connect_ex.return_value=errno.EINPROGRESS
        self.sock.getsockopt.return_value=errno.ECONNREFUSED
        with self.assertRaisesRegex(InvalidObservation,'connection_refused'):self.get()
        self.sock.close.assert_called_once();self.sock.send.assert_not_called()

    def test_stop_during_write_or_read_closes_owned_socket(self):
        for direction in ('write','read'):
            with self.subTest(direction=direction):
                self.stop=False;self.sock.reset_mock()
                def wait(r,w,x,timeout):
                    if bool(w)==(direction=='write'):self.stop=True
                    return r,w,x
                with patch.object(t.select,'select',side_effect=wait):
                    with self.assertRaisesRegex(InvalidObservation,'observation_stopped'):self.get()
                self.sock.__exit__.assert_called_once()

    def test_connect_and_partial_writes_share_deadline(self):
        self.sock.connect_ex.return_value=errno.EINPROGRESS
        self.sock.getsockopt.return_value=0
        def wait(r,w,x,timeout):self.clock+=.04;return r,w,x
        self.sock.send.return_value=1;self.sock.send.side_effect=None
        with patch.object(t.select,'select',side_effect=wait):
            with self.assertRaisesRegex(InvalidObservation,'http_deadline'):self.get(deadline=.1)
        self.sock.recv.assert_not_called();self.sock.__exit__.assert_called_once()

    def test_readable_eof_at_deadline_is_not_accepted(self):
        parts=iter([b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Type: application/json\r\n\r\n{}',b''])
        def recv(_size):
            part=next(parts)
            if not part:self.clock=.1
            return part
        self.sock.recv.side_effect=recv
        with self.assertRaisesRegex(InvalidObservation,'http_deadline'):self.get(deadline=.1)
        self.sock.__exit__.assert_called_once()

    def test_cancelled_poll_ticks_are_not_premature_timeout(self):
        intervals=[]
        def wait(r,w,x,timeout):
            intervals.append(timeout)
            if self.clock<.3:self.clock+=timeout;return [],[],[]
            return r,w,x
        with patch.object(t.select,'select',side_effect=wait):
            self.assertEqual(self.get()['body'],'{}')
        self.assertTrue(all(0<x<=.1 for x in intervals));self.assertGreaterEqual(self.clock,.3)

    def test_stop_takes_precedence_when_deadline_also_elapsed(self):
        self.stop=True;self.clock=1
        with self.assertRaisesRegex(InvalidObservation,'observation_stopped'):self.get(deadline=.1)
        self.sock.connect_ex.assert_not_called()


if __name__=='__main__':unittest.main(verbosity=2)

"""Join the native SIP and SRTP workers over the same loopback telephone call."""
import re
import secrets
import unittest

from tools.serve_live import test_sip_media as media
from tools.serve_live import test_sip_trunk as sip


class CarrierAudio(media.MediaFixture, unittest.TestCase):
    """The carrier uses only the SDP answer actually received over TLS."""


@unittest.skipUnless(sip.BINARY and media.WORKER, 'set both native SIP worker paths')
class DialogTest(sip.TrunkFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.audio = CarrierAudio()
        self.addCleanup(self.audio.doCleanups)
        self.audio.setUp()
        self.audio.start()
        self.carrier = self.peer(self.good)
        self.token = secrets.token_hex(8)
        self.offer = self.audio.offer().encode()
        self.carrier.send('INVITE', self.token, body=self.offer,
                          extra=b'Content-Type: application/sdp\r\n')
        self.tag = self.carrier.response(180, 'INVITE', self.token)
        event = self.event()
        self.assertEqual(event[:2], ['INCOMING', sip.SCOPE])
        self.live = event[2]
        self.assertNotIn(b'inline:', self.payload)
        self.control.send(f'OFFER lookup {sip.SCOPE} {self.live}'.encode())
        self.assertEqual(self.event(), ['OFFER', 'lookup'])
        self.assertTrue(self.payload == self.offer, 'SDP differs from TLS offer')
        self.audio.owner.send(b'O' + self.payload)
        reply = self.audio.owner.recv(65536)
        self.assertEqual(reply[:1], b'A')
        self.answer = reply[1:]

    def accept(self, scope, answer, result):
        self.control.send(f'ACCEPT op {scope} {self.live}\n'.encode() + answer)
        if result == 'submitted':
            self.assertEqual(self.event(), ['FINAL', sip.SCOPE, self.live, '200'])
        self.assertEqual(self.event(), ['RESULT', 'op', result])

    def answer_invite(self):
        self.accept(sip.SCOPE, self.answer, 'submitted')
        self.assertEqual(self.carrier.response(200, 'INVITE', self.token), self.tag)
        self.assertTrue(self.carrier.body == self.answer, 'SDP differs on the wire')
        self.assertEqual(self.carrier.headers['content-type'], 'application/sdp')
        self.target = re.search(r'(sips:[^> ]+)', self.carrier.headers['contact'])[1]
        self.audio.read_answer(self.carrier.body)

    def send_in_dialog(self, method, seq, tag=None, branch=None):
        branch = branch or secrets.token_hex(8)
        self.carrier.send(method, self.token, to_tag=tag or self.tag,
                          target=self.target, branch=branch, seq=seq)
        return branch

    def bye(self, seq=2):
        branch = self.send_in_dialog('BYE', seq)
        self.carrier.response(200, 'BYE', self.token, branch=branch, seq=seq)
        self.assertEqual(self.event(), ['ENDED', sip.SCOPE, self.live])

    def test_call_answer_ack_secure_audio_and_carrier_hangup(self):
        self.accept('other-customer', self.answer, 'not_found')
        self.accept(sip.SCOPE, b'invalid', 'invalid_answer')
        self.answer_invite()
        self.reject(sip.SCOPE, self.live, 486, 'conflict')
        self.carrier.send('INVITE', self.token, body=self.offer,
                          extra=b'Content-Type: application/sdp\r\n')
        self.carrier.response(200, 'INVITE', self.token)
        self.assertTrue(self.carrier.body == self.answer, 'SDP changed on retransmission')
        self.send_in_dialog('ACK', 1)
        self.assertEqual(self.event(), ['ACK', sip.SCOPE, self.live, '200'])
        self.audio.activate()
        self.send_in_dialog('ACK', 1)
        self.quiet()
        branch = self.send_in_dialog('BYE', 2, tag='unrelated')
        self.carrier.response(481, 'BYE', self.token, branch=branch, seq=2)
        self.quiet()
        self.audio.verify_audio('PCMU', False)
        self.bye()
        self.audio.drain()
        branch = self.send_in_dialog('BYE', 3)
        self.carrier.response(481, 'BYE', self.token, branch=branch, seq=3)
        self.quiet()

    def test_bye_after_answer_before_ack_does_not_activate_media(self):
        self.answer_invite()
        self.bye()
        self.send_in_dialog('ACK', 1)
        self.quiet()

    def local_hangup(self, scope, expect):
        command = f'HANGUP hang {scope} {self.live}'
        self.control.send(command.encode())
        if expect == 'submitted':
            self.assertEqual(self.event(), ['ENDING', sip.SCOPE, self.live])
        self.assertEqual(self.event(), ['RESULT', 'hang', expect])

    def receive_bye(self):
        reader = self.carrier.reader
        self.assertEqual(reader.readline(4096).split(),
                         [b'BYE', b'sips:phone@127.0.0.1:9', b'SIP/2.0'])
        fields = {}
        while True:
            line = reader.readline(8192)
            if line == b'\r\n':
                break
            self.assertTrue(line, 'incomplete BYE')
            key, value = line.rstrip().split(b':', 1)
            fields[key.lower()] = value.strip()
        self.assertEqual(fields[b'call-id'], (self.token + '@peer.test').encode())
        self.assertIn(('tag=' + self.tag).encode(), fields[b'from'])
        self.assertIn(('tag=' + self.token).encode(), fields[b'to'])
        self.assertRegex(fields[b'cseq'], rb'^[1-9][0-9]* BYE$')
        self.assertEqual(fields.get(b'content-length', b'0'), b'0')
        return fields

    def finish_local_hangup(self, status):
        self.answer_invite()
        self.local_hangup(sip.SCOPE, 'conflict')
        self.send_in_dialog('ACK', 1)
        self.assertEqual(self.event(), ['ACK', sip.SCOPE, self.live, '200'])
        self.local_hangup('other-customer', 'not_found')
        self.local_hangup(sip.SCOPE, 'submitted')
        fields = self.receive_bye()
        self.local_hangup(sip.SCOPE, 'conflict')
        self.quiet()
        def respond(code, reason):
            headers = [f'SIP/2.0 {code} {reason}'.encode()] + [
                key + b': ' + fields[key] for key in
                (b'via', b'from', b'to', b'call-id', b'cseq')]
            self.carrier.sock.sendall(b'\r\n'.join(headers + [
                b'Content-Length: 0', b'', b'']))
        respond(100, 'Trying')
        self.quiet()
        respond(status, 'OK' if status == 200 else 'Call Does Not Exist')
        self.assertEqual(self.event(), ['BYE_RESPONSE', sip.SCOPE,
                                        self.live, str(status)])
        self.assertEqual(self.event(), ['ENDED', sip.SCOPE, self.live])
        self.local_hangup(sip.SCOPE, 'not_found')

    def test_local_hangup_waits_for_final_carrier_response(self):
        self.finish_local_hangup(200)

    def test_remote_failure_is_preserved_on_local_hangup(self):
        self.finish_local_hangup(481)

    def test_carrier_disconnect_during_hangup_has_no_success_receipt(self):
        self.answer_invite()
        self.send_in_dialog('ACK', 1)
        self.assertEqual(self.event(), ['ACK', sip.SCOPE, self.live, '200'])
        self.local_hangup(sip.SCOPE, 'submitted')
        self.receive_bye()
        self.carrier.reader.close()
        self.carrier.sock.close()
        self.control.settimeout(36)
        self.assertEqual(self.event(), ['LOST', sip.SCOPE, self.live])
        self.local_hangup(sip.SCOPE, 'not_found')

    def test_cancel_before_answer_keeps_cancel_disposition(self):
        self.carrier.send('CANCEL', self.token)
        self.carrier.response(200, 'CANCEL', self.token)
        self.carrier.response(487, 'INVITE', self.token)
        self.assertEqual(self.event(), ['FINAL', sip.SCOPE, self.live, '487'])
        self.accept(sip.SCOPE, self.answer, 'conflict')
        self.carrier.send('ACK', self.token, to_tag=self.tag)
        self.assertEqual(self.event(), ['ACK', sip.SCOPE, self.live, '487'])
        self.accept(sip.SCOPE, self.answer, 'not_found')

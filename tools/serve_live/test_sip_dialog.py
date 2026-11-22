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
        return self.carrier.dialog_request('BYE', self.token, self.tag)

    def refer(self, target, result, scope=sip.SCOPE, op='r-first'):
        self.control.send(f'REFER {op} {scope} {self.live}\n{target}'.encode())
        self.assertEqual(self.event(), ['RESULT', op, result])

    def test_transfer_receipt_and_subscription_are_independent(self):
        target = 'sips:help@desk.invalid;transport=tls?Subject=A%20B'
        self.answer_invite()
        self.refer(target, 'conflict')
        self.send_in_dialog('ACK', 1)
        self.assertEqual(self.event(), ['ACK', sip.SCOPE, self.live, '200'])
        self.refer(target, 'not_found', 'other-customer')
        self.refer('tel:+12025550100\r\nExtra: bad', 'invalid_target')
        self.refer(target, 'submitted')
        fields = self.carrier.dialog_request('REFER', self.token, self.tag)
        self.assertEqual(fields[b'refer-to'], f'<{target}>'.encode())
        seq = fields[b'cseq'].split()[0].decode()
        notify = lambda n, event, state, body, **kw: self.carrier.transfer_notify(
            self.token, self.tag, self.target, n, event, state, body, **kw)
        self.carrier.reply_dialog(fields, 100)
        self.quiet()
        notify(2, 'refer;id=0', 'active', 'SIP/2.0 100 Trying\r\n', expected=481)
        notify(3, 'refer', 'active;expires=30', 'SIP/2.0 180 Ringing\r\n')
        self.refer(target, 'conflict')
        self.carrier.reply_dialog(fields, 202)
        self.assertEqual(self.event(), ['REFER_RESPONSE', 'r-first', '202'])
        self.refer(target, 'conflict')
        notify(4, 'refer;id='+seq, 'terminated', 'garbage', expected=400)
        notify(5, 'refer;id='+seq, 'terminated', 'SIP/2.0 486 Busy Here\r\n')
        self.assertEqual(self.event(), ['TRANSFER', sip.SCOPE, self.live, '486'])
        self.refer('tel:+12025550100', 'submitted', op='r-next')
        retry = self.carrier.dialog_request('REFER', self.token, self.tag)
        self.assertEqual(retry[b'refer-to'], b'<tel:+12025550100>')
        new = retry[b'cseq'].split()[0].decode()
        self.assertGreater(int(new), int(seq))
        notify(6, 'refer;id='+seq, 'terminated', 'SIP/2.0 200 OK\r\n', expected=481)
        notify(7, 'refer', 'terminated', 'SIP/2.0 200 OK\r\n', expected=481)
        notify(8, 'refer;id='+new, 'terminated', 'SIP/2.0 200 OK\r\n')
        self.assertEqual(self.event(), ['TRANSFER', sip.SCOPE, self.live, '200'])
        self.refer(target, 'conflict')
        self.carrier.reply_dialog(retry, 200)
        self.assertEqual(self.event(), ['REFER_RESPONSE', 'r-next', '200'])
        self.bye(seq=9)

    def test_expired_transfer_receipt_leaves_original_dialog_usable(self):
        self.answer_invite()
        self.send_in_dialog('ACK', 1)
        self.assertEqual(self.event(), ['ACK', sip.SCOPE, self.live, '200'])
        self.refer('tel:+12025550100', 'submitted')
        fields = self.carrier.dialog_request('REFER', self.token, self.tag)
        self.carrier.reply_dialog(fields, 202)
        self.assertEqual(self.event(), ['REFER_RESPONSE', 'r-first', '202'])
        event = 'refer;id=' + fields[b'cseq'].split()[0].decode()
        notify = self.carrier.transfer_notify
        notify(self.token, self.tag, self.target, 2, event, 'terminated',
               'SIP/2.0 200 OK\ninvalid', expected=400)
        notify(self.token, self.tag, self.target, 3, event, 'active;expires=0',
               'SIP/2.0 100 Trying\r\n')
        self.assertEqual(self.event(), ['TRANSFER', sip.SCOPE, self.live, '0'])
        notify(self.token, self.tag, self.target, 4, event, 'terminated',
               'SIP/2.0 200 OK\r\n', expected=481)
        self.bye(seq=5)

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

"""Negotiate a telephone-event payload and inspect independent, decrypted RTP."""
import os
from pathlib import Path
import re
import select
import struct
import tempfile
import time
import unittest

from tools.serve_live.test_sip_media import MediaFixture, WORKER


def with_events(offer, rate=8000, events='0-15', pt=101):
    offer = re.sub(r'^(m=audio [^\r]+)', rf'\g<1> {pt}', offer, flags=re.M)
    offer += f'a=rtpmap:{pt} telephone-event/{rate}\r\n'
    if events is not None:
        offer += f'a=fmtp:{pt} {events}\r\n'
    return offer


def send_key(audio, seq, timestamp, number, *, source=0x11334455, pt=101,
             end=False, duration=160, extended=False, corrupt=False):
    flags = 0x91 if extended else 0x80
    header = struct.pack('!BBHII',flags,pt | (0 if end else 128),seq,timestamp,source)
    if extended:
        header += struct.pack('!IHHI',199,0xbede,1,0)
    body = struct.pack('!BBH',number,10 | (128 if end else 0),duration)
    packet = audio.send.protect(header + body)
    if corrupt:
        packet = packet[:-1] + bytes([packet[-1] ^ 1])
    audio.rtp.sendto(packet, audio.target)


def read_key(audio, pt, number):
    deadline = time.monotonic()+3
    packets = []
    while time.monotonic() < deadline:
        if not select.select([audio.rtp], [], [], .08)[0]:
            continue
        clear = audio.receive.unprotect(audio.rtp.recv(65536))
        if clear[1] & 127 != pt:
            continue
        if len(clear) != 16 or clear[0] != 128 or clear[12] != number:
            raise AssertionError('unexpected telephone-event packet')
        seq, stamp, source = struct.unpack_from('!HII',clear,2)
        packets.append((seq, stamp, source, bool(clear[1]&128),
                        bool(clear[13]&128), int.from_bytes(clear[14:16],'big')))
        if sum(p[4] for p in packets) == 3:
            break
    if not packets or not packets[0][3] or not all(p[4] for p in packets[-3:]) or len(packets)<4:
        raise AssertionError(('missing start or repeated end reports', packets))
    if any(p[1:3] != packets[0][1:3] or p[5] < 1 for p in packets):
        raise AssertionError(('timestamp, source or duration changed',packets))
    if any((b[0]-a[0])%65536!=1 or b[5]<a[5] for a,b in zip(packets,packets[1:])):
        raise AssertionError(('sequence or duration',packets))
    return packets


@unittest.skipUnless(WORKER, 'set Q4T_SIP_MEDIA_WORKER')
class DtmfMedia(MediaFixture, unittest.TestCase):
    def test_receive_authentication_timestamp_and_repeated_ends(self):
        self.start()
        answer = self.negotiate(with_events(self.offer(), events='0-11,16-32'))
        self.assertIn('a=fmtp:101 0-11\r\n',answer)
        send_key(self,1,117,1)
        self.activate()
        send_key(self,2,118,3,corrupt=True)
        send_key(self,3,119,15)
        self.assertFalse(select.select([self.owner],[],[],.06)[0])
        send_key(self,4,212,11,extended=True)
        self.assertEqual(self.owner.recv(16), b'K#')
        for seq in range(5,8):
            send_key(self,seq,212,11,end=True,duration=960)
        send_key(self,8,2212,11,end=True,duration=480)
        self.assertEqual(self.owner.recv(16), b'K#')
        self.assertFalse(select.select([self.owner],[],[],.08)[0])
        self.drain()

    def exercise_sender(self, codec, rate):
        self.start()
        answer = self.negotiate(with_events(self.offer(codec),rate))
        self.assertIn(f'a=rtpmap:101 telephone-event/{rate}\r\n',answer)
        self.activate()
        self.owner.send(b'K*')
        sent = read_key(self,101,10)
        self.assertGreaterEqual(sent[-1][5],rate//20)
        self.assertEqual(self.owner.recv(16), b'T*')
        self.owner.send(b'K*')
        again = read_key(self,101,10)
        self.assertNotEqual(again[0][1],sent[0][1])
        self.assertEqual(self.owner.recv(16), b'T*')
        # The voice source must still transmit after two keys.
        for _ in range(6):
            self.owner.send(b'P'+bytes(960))
            time.sleep(.02)
        self.assertTrue(select.select([self.rtp],[],[],1)[0])
        voice = self.receive.unprotect(self.rtp.recv(65536))
        self.assertEqual(voice[1]&127, 0 if codec=='PCMU' else 111)
        source = int.from_bytes(voice[8:12],'big')
        if rate == (8000 if codec=='PCMU' else 48000):
            self.assertEqual(source,again[0][2])
            self.assertEqual((int.from_bytes(voice[2:4],'big')-again[-1][0])%65536,1)
        else:
            self.assertNotEqual(source,again[0][2])
        self.drain()

    def test_g711_shared_clock(self):
        self.exercise_sender('PCMU',8000)

    def test_opus_shared_clock(self):
        self.exercise_sender('OPUS',48000)

    def test_opus_and_8khz_events(self):
        self.exercise_sender('OPUS',8000)

    def test_prefer_common_clock_and_default_dtmf_set(self):
        self.start()
        offer = with_events(with_events(self.offer('OPUS'),8000),48000,None,102)
        answer = self.negotiate(offer)
        self.assertIn('a=rtpmap:102 telephone-event/48000\r\n',answer)
        self.assertIn('a=fmtp:102 0-15\r\n',answer)
        self.assertNotIn('a=rtpmap:101',answer)
        self.activate()
        self.drain()

    def test_packed_reports_and_long_event_continuation(self):
        self.start()
        self.negotiate(with_events(self.offer()))
        self.activate()
        def packet(seq, stamp, entries):
            head = struct.pack('!BBHII',128,101,seq,stamp,0x11334455)
            data = b''.join(struct.pack('!BBH',*v) for v in entries)
            self.rtp.sendto(self.send.protect(head+data), self.target)
        packet(0, 1000, [(1,138,200),(2,138,0)])
        self.assertFalse(select.select([self.owner],[],[],.05)[0])
        packet(1, 2000, [(1,138,200),(2,138,200)])
        self.assertEqual(self.owner.recv(16),b'K1')
        self.assertEqual(self.owner.recv(16),b'K2')
        packet(2, 2000, [(1,138,200),(2,138,200)])
        packet(3, 0xffffff00, [(13,10,65535)])
        self.assertEqual(self.owner.recv(16),b'KB')
        packet(4, (0xffffff00+65535)%2**32, [(13,138,400)])
        self.assertFalse(select.select([self.owner],[],[],.08)[0])
        self.drain()

    def test_direction_limits_apply_to_keys(self):
        for direction in ('sendonly','recvonly','inactive'):
            with self.subTest(direction=direction):
                self.start()
                offer = with_events(self.offer('OPUS'),8000) + f'a={direction}\r\n'
                answer = self.negotiate(offer)
                self.assertEqual('telephone-event' in answer, direction != 'inactive')
                self.activate()
                send_key(self,0,32000,2)
                if direction == 'sendonly':
                    self.assertEqual(self.owner.recv(16), b'K2')
                else:
                    self.assertFalse(select.select([self.owner],[],[],.06)[0])
                if direction != 'recvonly':
                    self.owner.send(b'K3')
                    self.assertEqual(self.owner.recv(16), b'U3')
                self.drain()

    def test_partial_keyset_and_voice_only(self):
        for events in ('1-3,11', '20-32', '11-2'):
            with self.subTest(events=events):
                self.start()
                answer = self.negotiate(with_events(self.offer(),events=events))
                self.assertEqual('telephone-event' in answer, events=='1-3,11')
                self.activate()
                self.owner.send(b'KD')
                self.assertEqual(self.owner.recv(16), b'UD')
                self.drain()

    def test_missing_optional_generator_preserves_voice(self):
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(dir=root/'.q4t-work') as work:
            plugins = Path(work)/'plugins'
            plugins.mkdir()
            installed = Path('/usr/lib/aarch64-linux-gnu/gstreamer-1.0')
            self.assertTrue((installed/'libgstdtmf.so').is_file())
            for p in installed.glob('*.so'):
                if p.name != 'libgstdtmf.so':
                    (plugins/p.name).symlink_to(p)
            self.start(env=dict(os.environ,GST_PLUGIN_PATH_1_0='',
                GST_PLUGIN_SYSTEM_PATH_1_0=str(plugins),GST_REGISTRY_1_0=str(Path(work)/'registry')))
            answer = self.negotiate(with_events(self.offer()))
            self.assertNotIn('telephone-event',answer)
            self.activate()
            self.verify_audio('PCMU',False)
            self.drain()

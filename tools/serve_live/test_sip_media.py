"""Independent local SRTP carrier. Keys stay in memory; no model or GPU."""
import array
import base64
import contextlib
import ctypes
import ctypes.util
import math
import os
import re
import secrets
import select
import socket
import struct
import subprocess
import time
import unittest

import pylibsrtp

WORKER = os.environ.get('Q4T_SIP_MEDIA_WORKER')
SUITE = 'AES_CM_128_HMAC_SHA1_80'


def tone(hz, rate, frame):
    return array.array('h', (round(9000*math.sin(2*math.pi*hz*(frame*rate//50+i)/rate))
                            for i in range(rate//50))).tobytes()


def energy(pcm, rate, hz):
    samples = array.array('h')
    samples.frombytes(pcm)
    phase = 2*math.pi*hz/rate
    return 2*abs(sum(s*complex(math.cos(i*phase), math.sin(i*phase))
                     for i,s in enumerate(samples)))/len(samples)


def session(key, inbound):
    policy = pylibsrtp.Policy
    return pylibsrtp.Session(policy(key=key, ssrc_type=policy.SSRC_ANY_INBOUND
                                    if inbound else policy.SSRC_ANY_OUTBOUND))


class Codec:
    def __init__(self, name, cleanup):
        self.name = name
        self.rate = 48000 if name == 'OPUS' else 8000
        self.pt = {'PCMU':0,'PCMA':8,'OPUS':111}[name]
        if name == 'OPUS':
            lib = self.lib = ctypes.CDLL(ctypes.util.find_library('opus'))
            ptr, integer = ctypes.c_void_p, ctypes.c_int
            lib.opus_encoder_create.argtypes = [integer,integer,integer,ctypes.POINTER(integer)]
            lib.opus_decoder_create.argtypes = [integer,integer,ctypes.POINTER(integer)]
            lib.opus_encoder_create.restype = lib.opus_decoder_create.restype = ptr
            lib.opus_encode.argtypes = [ptr,ptr,integer,ptr,integer]
            lib.opus_decode.argtypes = [ptr,ptr,integer,ptr,integer,integer]
            lib.opus_encoder_destroy.argtypes = lib.opus_decoder_destroy.argtypes = [ptr]
            error = integer()
            self.enc = lib.opus_encoder_create(self.rate,1,2049,ctypes.byref(error))
            self.dec = lib.opus_decoder_create(self.rate,1,ctypes.byref(error))
            if not self.enc or not self.dec:
                raise RuntimeError('system Opus codec unavailable')
            cleanup(lib.opus_encoder_destroy, self.enc)
            cleanup(lib.opus_decoder_destroy, self.dec)
        else:
            import audioop
            self.encode = (lambda data: audioop.lin2ulaw(data,2)) if name=='PCMU' else (
                lambda data: audioop.lin2alaw(data,2))
            self.decode = (lambda data: audioop.ulaw2lin(data,2)) if name=='PCMU' else (
                lambda data: audioop.alaw2lin(data,2))

    def encode(self, data):
        buf = ctypes.create_string_buffer(1500)
        n = self.lib.opus_encode(self.enc,data,len(data)//2,buf,len(buf))
        if n<=0: raise ValueError('Opus encode')
        return buf.raw[:n]

    def decode(self, data):
        buf = ctypes.create_string_buffer(11520)
        n = self.lib.opus_decode(self.dec,data,len(data),buf,len(buf)//2,0)
        if n<=0: raise ValueError('Opus decode')
        return buf.raw[:2*n]


class MediaFixture:
    def setUp(self):
        self.stack=contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.key=secrets.token_bytes(30)
        self.rtp=self.stack.enter_context(socket.socket(socket.AF_INET,socket.SOCK_DGRAM))
        self.rtcp=self.stack.enter_context(socket.socket(socket.AF_INET,socket.SOCK_DGRAM))
        for s in (self.rtp,self.rtcp): s.bind(('127.0.0.1',0))

    def start(self, env=None, ip='127.0.0.1', peer='127.0.0.1'):
        owner, child=socket.socketpair(socket.AF_UNIX,socket.SOCK_SEQPACKET)
        self.owner=self.stack.enter_context(owner)
        self.owner.settimeout(4)
        with child:
            self.proc=subprocess.Popen([WORKER,str(child.fileno()),ip,peer],pass_fds=(child.fileno(),),env=env)
        def stop(proc=self.proc):
            if proc.poll() is None:
                proc.kill()
                proc.wait(2)
        self.stack.callback(stop)

    def offer(self, codec='PCMU', mux=False):
        key=base64.b64encode(self.key).decode()
        pt={'PCMU':0,'PCMA':8,'OPUS':111}[codec]
        media=f'm=audio {self.rtp.getsockname()[1]} RTP/SAVP {pt}\r\n'
        control='a=rtcp-mux\r\n' if mux else f'a=rtcp:{self.rtcp.getsockname()[1]} IN IP4 127.0.0.1\r\n'
        return ('v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=carrier\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n'+
                media+f'a=rtpmap:{pt} {codec}/'+('48000/2' if codec=='OPUS' else '8000')+
                '\r\n'+control+f'a=crypto:7 {SUITE} inline:{key}\r\n')

    def negotiate(self, wire, mux=False):
        self.owner.send(b'O'+wire.encode())
        reply=self.owner.recv(65536)
        self.assertEqual(reply[:1], b'A')
        return self.read_answer(reply[1:], mux)

    def read_answer(self, wire, mux=False):
        s=wire.decode()
        port=int(re.search(r'm=audio (\d+) RTP/SAVP',s)[1])
        control=port if mux else int(re.search(r'a=rtcp:(\d+)',s)[1])
        self.assertTrue(('a=rtcp-mux' if mux else 'a=rtcp:') in s, 'RTCP endpoint missing')
        key=base64.b64decode(re.search(r'a=crypto:7 '+SUITE+r' inline:([^\r]+)',s)[1],validate=True)
        self.assertTrue(len(key)==30 and key!=self.key, 'fresh directional key')
        self.target=('127.0.0.1',port)
        self.control_target=('127.0.0.1',control)
        self.receive=session(key,True)
        self.send=session(self.key,False)
        return s

    def activate(self):
        self.owner.send(b'G')
        self.assertEqual(self.owner.recv(65536),b'V')

    def exchange(self,name,mux):
        self.start()
        self.negotiate(self.offer(name,mux),mux)
        self.activate()
        self.verify_audio(name, mux)
        self.drain()

    def verify_audio(self, name, mux):
        codec=Codec(name,self.stack.callback)
        decoded, captured=bytearray(),bytearray()
        source=0x11223344
        lsr=0x12345678
        acknowledged=False
        report=struct.pack('!BBHIIIIII',128,200,6,source,0x00001234,0x56780000,0,20,3200)
        report+=struct.pack('!BBHI',129,202,3,source)+b'\x01\x04peer\0\0'
        feedback=self.rtp if mux else self.rtcp
        seq=65000
        start=time.monotonic()
        sent=0
        while time.monotonic()-start<6:
            now=time.monotonic()
            if sent<160 and now>=start+sent*.02:
                data=codec.encode(tone(440,codec.rate,sent))
                plain=struct.pack('!BBHII',128,codec.pt,(seq+sent)%65536,32000+sent*(codec.rate//50),source)+data
                self.rtp.sendto(self.send.protect(plain),self.target)
                self.owner.send(b'P'+tone(720,24000,sent))
                if sent in (6,30,75):
                    feedback.sendto(self.send.protect_rtcp(report),self.control_target)
                sent+=1
            ready,_,_=select.select([self.rtp,self.rtcp,self.owner],[],[],.008)
            for s in ready:
                pkt=s.recv(65536)
                if s==self.owner:
                    self.assertEqual(pkt[:1],b'P')
                    captured.extend(pkt[1:])
                elif s==self.rtcp or (mux and 192<=pkt[1]<=223):
                    clear=self.receive.unprotect_rtcp(pkt)
                    offset=0
                    while offset+4<=len(clear):
                        first,kind,words=struct.unpack_from('!BBH',clear,offset)
                        size=(words+1)*4
                        self.assertLessEqual(offset+size,len(clear))
                        if kind in (200,201):
                            at=offset+(28 if kind==200 else 8)
                            for _ in range(first&31):
                                ssrc,=struct.unpack_from('!I',clear,at)
                                last,=struct.unpack_from('!I',clear,at+16)
                                acknowledged |= ssrc==source and last==lsr
                                at+=24
                        offset+=size
                else:
                    clear=self.receive.unprotect(pkt)
                    self.assertEqual(clear[1]&127,codec.pt)
                    header=12+4*(clear[0]&15)
                    self.assertFalse(clear[0]&0x10)
                    decoded.extend(codec.decode(clear[header:]))
            if acknowledged and len(decoded)>=codec.rate and len(captured)>=24000: break
        self.assertTrue(acknowledged,'peer received SRTCP report for its authenticated sender report')
        for pcm,rate,hz,other in [(captured,24000,440,720),(decoded,codec.rate,720,440)]:
            self.assertGreaterEqual(len(pcm),rate)
            body=pcm[rate//4:rate]
            self.assertGreater(energy(body,rate,hz),5500)
            self.assertLess(energy(body,rate,other),1500)

    def drain(self):
        self.owner.send(b'X')
        for _ in range(128):
            packet=self.owner.recv(65536)
            if packet==b'X': break
            self.assertEqual(packet[:1],b'P')
        self.assertEqual(packet,b'X')
        self.assertEqual(self.proc.wait(3),0)
        for port in set((self.target[1],self.control_target[1])):
            with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as unused:
                unused.bind(('127.0.0.1',port))


@unittest.skipUnless(WORKER, 'set Q4T_SIP_MEDIA_WORKER')
class MediaTest(MediaFixture, unittest.TestCase):
    def test_pcmu_with_separate_rtcp(self):
        self.exchange('PCMU',False)
    def test_pcma_with_multiplexed_rtcp(self):
        self.exchange('PCMA',True)
    def test_opus_with_separate_rtcp(self):
        self.exchange('OPUS',False)

    def test_declines_cleartext_bad_key_and_unapproved_media_address(self):
        good=self.offer()
        cases=[good.replace('RTP/SAVP','RTP/AVP'),
               good.replace(SUITE,'UNRECOGNIZED'),
               good.replace('inline:', 'inline:bad'),
               good.replace('c=IN IP4 127.0.0.1','c=IN IP4 127.0.0.2'),
               good.replace('IN IP4 127.0.0.1\r\na=crypto','IN IP4 127.0.0.2\r\na=crypto'),
               good.replace('a=crypto:7', 'a=crypto:7 OTHER inline:x\r\na=crypto:7'),
               good+'a=sendonly\r\na=recvonly\r\n', good.replace('PCMU/8000','OPUS/48000/2')]
        for index,wire in enumerate(cases):
            with self.subTest(index=index):
                self.start()
                self.owner.send(b'O'+wire.encode())
                self.assertEqual(self.owner.recv(256),b'I')
                self.assertEqual(self.proc.wait(3),1)

    def test_early_audio_wrong_source_key_and_replayed_packets_remain_silent(self):
        self.start()
        self.negotiate(self.offer())
        codec=Codec('PCMU',self.stack.callback)
        source=0x42345678
        bogus=session(secrets.token_bytes(30),False)
        def batch(sender,start):
            return [sender.protect(struct.pack('!BBHII',128,0,i,160*i,source)+
                    codec.encode(tone(440,8000,i))) for i in range(start,start+12)]
        early=batch(self.send,10)
        for pkt in early: self.rtp.sendto(pkt,self.target)
        self.assertFalse(select.select([self.owner],[],[],.15)[0])
        self.activate()
        wrong=self.stack.enter_context(socket.socket(socket.AF_INET,socket.SOCK_DGRAM))
        for pkt in batch(self.send,25): wrong.sendto(pkt,self.target)
        for pkt in batch(bogus,40): self.rtp.sendto(pkt,self.target)
        self.assertFalse(select.select([self.owner],[],[],.25)[0])
        good=batch(self.send,60)
        for pkt in good: self.rtp.sendto(pkt,self.target);time.sleep(.02)
        pcm=bytearray()
        while select.select([self.owner],[],[],.22)[0]:
            frame=self.owner.recv(65536)
            self.assertEqual(frame[:1],b'P')
            pcm.extend(frame[1:])
        self.assertGreater(len(pcm),6000)
        for pkt in good: self.rtp.sendto(pkt,self.target)
        self.assertFalse(select.select([self.owner],[],[],.22)[0])

    def test_ipv6_static_mapping_and_declined_video_line(self):
        self.start(ip='::1',peer='::1')
        wire=self.offer().replace('IN IP4 127.0.0.1','IN IP6 ::1')
        wire=wire.replace('a=rtpmap:0 PCMU/8000\r\n','')
        wire=wire.replace('m=audio','m=video 49130 RTP/SAVP 98\r\nm=audio')
        self.owner.send(b'O'+wire.encode())
        reply=self.owner.recv(65536)
        self.assertEqual(reply[:1],b'A')
        self.assertTrue(b'c=IN IP6 ::1\r\n' in reply, 'IPv6 local answer')
        self.assertTrue(b'm=video 0 RTP/SAVP 98' in reply, 'video must be declined')
        self.assertLess(reply.index(b'm=video'), reply.index(b'm=audio'))
        self.assertTrue(b'a=rtpmap:0 PCMU/8000' in reply, 'static audio mapping')
        self.owner.send(b'X')
        self.assertEqual(self.owner.recv(64),b'X')
        self.assertEqual(self.proc.wait(2),0)

    def test_remote_sendonly_offer_has_recvonly_answer(self):
        self.start()
        answer=self.negotiate(self.offer()+'a=sendonly\r\n')
        self.assertTrue('a=recvonly\r\n' in answer, 'negotiated direction')
        self.activate()
        for i in range(10): self.owner.send(b'P'+tone(500,24000,i))
        self.assertFalse(select.select([self.rtp],[],[],.25)[0])
        self.owner.send(b'X')
        self.assertEqual(self.owner.recv(64),b'X')
        self.assertEqual(self.proc.wait(2),0)

    def test_final_audio_burst_drains_before_ack(self):
        self.start()
        codec=Codec('PCMU',self.stack.callback)
        self.negotiate(self.offer())
        self.activate()
        # A short burst occupies the resampler and RTP payloader at close.
        for i in range(12):
            self.owner.send(b'P'+tone(600 if i<8 else 1200,24000,i))
        self.owner.send(b'X')
        self.assertEqual(self.owner.recv(64),b'X')
        self.assertEqual(self.proc.wait(3),0)
        pcm=bytearray()
        while select.select([self.rtp],[],[],.05)[0]:
            pkt=self.receive.unprotect(self.rtp.recv(65536))
            pcm.extend(codec.decode(pkt[12:]))
        self.assertGreaterEqual(len(pcm)//2,12*160-1)
        self.assertGreater(energy(pcm[-640:],8000,1200),6500)

    def test_absent_codec_plugins_are_local_media_failure(self):
        self.start(env=dict(os.environ,GST_PLUGIN_PATH_1_0='',GST_PLUGIN_SYSTEM_PATH_1_0='',
                            GST_REGISTRY_1_0=os.devnull,GST_REGISTRY_UPDATE='no'))
        self.owner.send(b'O'+self.offer().encode())
        self.assertEqual(self.owner.recv(64),b'E')
        self.assertEqual(self.proc.wait(3),1)

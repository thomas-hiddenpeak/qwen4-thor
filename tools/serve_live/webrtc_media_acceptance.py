"""Bounded CPU WebRTC peer for the native media worker, using loopback ICE."""
import argparse
import array
import json
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time

import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstWebRTC', '1.0')
gi.require_version('GstSdp', '1.0')
from gi.repository import Gst, GstSdp, GstWebRTC


def require(value, message):
    if not value:
        raise ValueError(message)


def action(element, name, *args):
    promise = Gst.Promise.new()
    element.emit(name, *args, promise)
    require(promise.wait() == Gst.PromiseResult.REPLIED, name)
    reply = promise.get_reply()
    require(reply is None or not reply.has_field('error'), name)
    return promise


def wait_for(probe, message, seconds=6):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        result = probe()
        if result:
            return result
        time.sleep(.005)
    raise TimeoutError(message)


def tone(hz):
    return array.array('h', [round(9000*math.sin(2*math.pi*i*hz/24000))
                              for i in range(480)]).tobytes()


def amplitude(audio, hz):
    data = array.array('h')
    data.frombytes(audio)
    n = len(data)
    phase = 2*math.pi*hz/24000
    return 2*math.hypot(sum(v*math.sin(i*phase) for i,v in enumerate(data)),
                        sum(v*math.cos(i*phase) for i,v in enumerate(data)))/n


def push(source, data):
    buf = Gst.Buffer.new_allocate(None, len(data), None)
    buf.fill(0, data)
    buf.duration = Gst.SECOND*len(data)//(2*24000)
    require(source.emit('push-buffer', buf) == Gst.FlowReturn.OK, 'peer PCM push')


def packet_child(binary, env=None, address='127.0.0.1'):
    owner, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        proc = subprocess.Popen([binary, str(child.fileno()), address],
                                pass_fds=(child.fileno(),), env=env)
    except BaseException:
        owner.close()
        raise
    finally:
        child.close()
    return proc, owner


def control(sock, expected):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        packet = sock.recv(65536)
        if packet == expected:
            return
        require(packet[:1] == b'P' and len(packet)%2 == 1, 'unexpected media control')
    raise TimeoutError('media control')


def exchange(binary, codec, wrong_pin=False, missing_plugins=False):
    formats = {'OPUS':('opusenc', 'opusdec', 'rtpopuspay', 'rtpopusdepay', 48000, 111),
               'PCMU':('mulawenc','mulawdec','rtppcmupay','rtppcmudepay',8000,0),
               'PCMA':('alawenc','alawdec','rtppcmapay','rtppcmadepay',8000,8)}
    enc, dec, pay, depay, rate, pt = formats[codec]
    pipe = Gst.parse_launch('webrtcbin name=rtc bundle-policy=max-bundle '
        'appsrc name=src is-live=true do-timestamp=true format=time '
        'caps=audio/x-raw,format=S16LE,layout=interleaved,channels=1,rate=24000 '
        f'! audioconvert ! audioresample ! audio/x-raw,rate={rate} ! {enc} '
        f'! {pay} pt={pt} ! application/x-rtp,media=audio,encoding-name={codec},'
        f'payload=(int){pt},clock-rate=(int){rate} ! rtc. '
        f'{depay} name=depay ! {dec} ! audioconvert ! audioresample ! '
        'audio/x-raw,format=S16LE,layout=interleaved,channels=1,rate=24000 '
        '! appsink name=sink sync=false async=false max-buffers=128')
    peer = pipe.get_by_name('rtc')
    ice = peer.get_property('ice-agent')
    # In 1.24 webrtcbin keeps its initial ICE reference floating. PyGObject
    # sinks it when wrapping the property, leaving one reference for two
    # owners. Restore the bin's reference before starting either peer.
    if ice.__grefcount__ == 1:
        ice._ref()
    nice = ice.get_property('agent')
    if nice.find_property('upnp'):
        nice.set_property('upnp',False)
    require(ice.emit('add-local-ip-address','127.0.0.1'), 'bind loopback')
    ice.set_property('ice-tcp',False)
    source, sink = pipe.get_by_name('src'), pipe.get_by_name('sink')
    receive = pipe.get_by_name('depay').get_static_pad('sink')
    peer.connect('pad-added', lambda _, pad: pad.link(receive)
                 if pad.get_direction() == Gst.PadDirection.SRC else None)
    env = None
    if missing_plugins:
        env = dict(os.environ, GST_PLUGIN_PATH_1_0='', GST_PLUGIN_SYSTEM_PATH_1_0='',
                   GST_REGISTRY_1_0=os.devnull, GST_REGISTRY_UPDATE='no')
    proc, sock = packet_child(binary,env)
    try:
        require(pipe.set_state(Gst.State.PLAYING) != Gst.StateChangeReturn.FAILURE,
                'peer startup')
        channel = peer.emit('create-data-channel','oai-events',None)
        require(channel is not None, 'peer datachannel')
        messages = []
        channel.connect('on-message-string',lambda _, value: messages.append(value))
        push(source,tone(400))
        created = action(peer,'create-offer',None)
        reply = created.get_reply()
        offer = reply.get_value('offer')
        action(peer,'set-local-description',offer)
        wait_for(lambda: peer.get_property('ice-gathering-state') ==
                 GstWebRTC.WebRTCICEGatheringState.COMPLETE, 'peer ICE gathering')
        gathered = peer.get_property('local-description')
        sdp = gathered.sdp.as_text()
        if wrong_pin:
            sdp, count = re.subn(r'(a=fingerprint:sha-256 )([0-9A-Fa-f])',
                lambda m: m[1]+('1' if m[2]=='0' else '0'),sdp)
            require(count > 0, 'test offer contains certificate fingerprint')
        sock.settimeout(5)
        sock.sendall(b'O'+sdp.encode())
        answer = sock.recv(65536)
        if missing_plugins:
            require(answer == b'E' and proc.wait(timeout=3) == 1,'missing plugin isolation')
            return
        require(answer[:1] == b'A', 'worker SDP answer: '+repr(answer[:10]))
        secure = answer[1:].decode()
        candidates = [v for v in secure.splitlines() if v.startswith('a=candidate:')]
        require(candidates and all(row.split()[4] == '127.0.0.1' for row in candidates)
                and 'UDP/TLS/RTP/SAVPF' in secure and 'UDP/DTLS/SCTP' in secure
                and 'a=fingerprint:sha-256' in secure.lower(),
                'negotiated secure loopback media '+repr(
                    [v for v in secure.splitlines() if v.startswith('m=')] +
                    [row.split()[4] for row in candidates]))
        status, desc = GstSdp.SDPMessage.new_from_text(secure)
        require(status == GstSdp.SDPResult.OK, 'parse answer')
        action(peer,'set-remote-description',GstWebRTC.WebRTCSessionDescription.new(
            GstWebRTC.WebRTCSDPType.ANSWER,desc))
        if wrong_pin:
            require(sock.recv(65536) == b'E' and proc.wait(timeout=3) == 1,
                    'foreign certificate must not publish authenticated media or control')
            return
        require(sock.recv(65536) == b'V', 'worker verified DTLS certificate')
        control(sock,b'R')
        wait_for(lambda: channel.get_property('ready-state') ==
                 GstWebRTC.WebRTCDataChannelState.OPEN, 'peer open')
        channel.send_string('{"type":"session.usage.get"}')
        control(sock,b'D{"type":"session.usage.get"}')
        sock.sendall(b'D{"type":"session.usage.updated"}')
        wait_for(lambda: messages == ['{"type":"session.usage.updated"}'],'wire response')
        channel.close()
        control(sock,b'C')
        inbound, outbound = bytearray(), bytearray()
        sock.setblocking(False)
        peer_pcm, worker_pcm = tone(400), tone(850)
        for i in range(100):
            if i < 60:
                push(source,peer_pcm)
                sock.sendall(b'P'+worker_pcm)
            time.sleep(.02)
            while True:
                try:
                    sample = sock.recv(65536)
                except BlockingIOError:
                    break
                require(sample[:1] == b'P', 'worker media frame: '+repr(sample[:10]))
                inbound.extend(sample[1:])
            while (sample := sink.emit('try-pull-sample',0)) is not None:
                buf = sample.get_buffer()
                outbound.extend(buf.extract_dup(0,buf.get_size()))
        amplitudes = []
        for audio, expected, other in ((inbound,400,850),(outbound,850,400)):
            require(len(audio) >= 24000,'at least half a second actual decoded audio')
            # Steady middle segment bypasses jitter-buffer start and codec padding.
            mid = len(audio)//4*2
            clip = audio[mid-4800:mid+4800]
            wanted, alternate = amplitude(clip,expected), amplitude(clip,other)
            require(wanted > 3000 and wanted > 5*alternate, 'decoded tone identity')
            amplitudes.append(round(wanted))
        sock.sendall(b'X')
        require(proc.wait(timeout=4) == 0,'worker normal close')
        return dict(codec=codec, payload=pt, inbound_samples=len(inbound)//2,
                    outbound_samples=len(outbound)//2, amplitudes=amplitudes,
                    loopback_ice_candidates=len(candidates), data_messages=2,
                    audio_after_data_channel_close=True)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        sock.close()
        pipe.set_state(Gst.State.NULL)


def reject(binary, packet):
    proc, sock = packet_child(binary)
    try:
        sock.settimeout(4)
        sock.sendall(packet)
        require(sock.recv(16) == b'E' and proc.wait(timeout=3) == 1,
                'invalid offer or ordering accepted')
    finally:
        sock.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--codec',choices=['OPUS','PCMU','PCMA'],action='append')
    args = parser.parse_args()
    Gst.init(None)
    binary = str(args.worker.resolve(strict=True))
    require(sys.byteorder == 'little', 'S16LE acceptance fixture')
    rejects = [b'Dbefore-offer',b'Ov=0\r\nm=audio 9 RTP/AVP 0\r\n',
               b'Ov=0\x00invalid',b'O'+b'a'*65536]
    for pkt in rejects:
        reject(binary,pkt)
    for address in ('0.0.0.0', '::', '224.0.0.1', 'localhost'):
        proc, sock = packet_child(binary,address=address)
        sock.close()
        require(proc.wait(timeout=3) == 2, 'invalid ICE binding')
    proc, sock = packet_child(binary)
    sock.close()
    require(proc.wait(timeout=3) == 0, 'lost owner releases worker')
    proc, sock = packet_child(binary)
    started = time.monotonic()
    try:
        sock.settimeout(18)
        require(sock.recv(16) == b'E' and proc.wait(timeout=2) == 1,
                'worker startup timeout')
        idle = round(time.monotonic()-started,3)
        require(14.5 <= idle < 18, 'bounded startup')
    finally:
        sock.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    exchange(binary,'OPUS',wrong_pin=True)
    exchange(binary,'OPUS',missing_plugins=True)
    result = dict(media=[exchange(binary,c) for c in args.codec or ['OPUS','PCMU','PCMA']],
                  rejected=len(rejects),rejected_wrong_pin=True,isolated_missing_plugins=True,
                  invalid_bindings=4,owner_eof_cleanup=True,startup_timeout_seconds=idle,
                  new_model_starts=0)
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()

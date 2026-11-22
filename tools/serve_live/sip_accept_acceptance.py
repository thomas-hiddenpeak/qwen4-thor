"""Confirm a scoped inbound SIP call through native TLS, Opus and the official SDK."""
import argparse
import array
import audioop
import contextlib
import json
import os
from pathlib import Path
import re
import secrets
import select
import signal as signals
import socket
import ssl
import struct
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_live import responses_acceptance as live
from tools.serve_live import sip_reject_acceptance as signal
from tools.serve_live.client_acceptance import retained_query
from tools.serve_live.test_sip_dialog import CarrierAudio
from tools.serve_live.test_sip_media import Codec
from tools.serve_live.test_sip_trunk import Peer
from tools.serve_realtime import transcription_acceptance as base
from tools.serve_recovery import profile_acceptance as qualify
from tools.serve_service.package import read, require, save, sha

LIMITS = dict(starts=1, ingress_starts=1, calls=1, sdk_requests=6, sidebands=1,
              voice_inputs=1, client_results=1, chats=1, controls=2000, seconds=320)
SCHEMA = 'q4t-live-sip-accept-business-v1'


class Stage(signal.Stage):
    limits = LIMITS
    subscriber_handles_reject = False

    def extra_model_args(self):
        return super().extra_model_args() + live.Stage.extra_model_args(self)

    def wait_file(self, file, sdk, seconds):
        end = time.monotonic() + seconds
        while not file.is_file() and time.monotonic() < end and sdk.poll() is None:
            time.sleep(.01)
        require(file.is_file(), f'SDK step {file.name} unavailable, exit {sdk.poll()}')

    def client(self, live_id, stack, blocked_media=False):
        log = stack.enter_context((self.out/('unready-sdk.log' if blocked_media else 'sdk.log')).open('xb'))
        proc = subprocess.Popen(['node', ROOT/'tools/serve_live/sip_accept_sdk.mjs'],
            stdin=subprocess.PIPE, stdout=log, stderr=log,
            env={**os.environ, 'NODE_EXTRA_CA_CERTS':self.plan['tls']['certificate']})
        def finish():
            if proc.poll() is None:
                proc.kill()
            proc.wait(3)
        stack.callback(finish)
        args = dict(id=live_id, blocked_media=blocked_media, port=self.plan['public_port'], key=self.keys['alpha'],
                    other=self.keys['beta'], out=str(self.out), record=self.plan['record'],
                    sdk=self.plan['sdk_directory'], ca=self.plan['tls']['certificate'])
        with proc.stdin:
            proc.stdin.write(json.dumps(args).encode())
        return proc

    def speech(self, audio, codec, sdk):
        clip = retained_query()
        require(clip == self.plan['retained_query'], 'retained input identity')
        # The consumer and the session exchange 24 kHz PCM; Opus RTP runs at 48 kHz.
        raw = bytes(14400) + Path(clip['path']).read_bytes() + bytes(76800)
        samples, _ = audioop.ratecv(raw, 2, 1, 24000, 48000, None)
        offset = 0
        start = time.monotonic()
        seq = 0
        while offset < len(samples):
            frame = samples[offset:offset+1920].ljust(1920, b'\0')
            encoded = codec.encode(frame)
            header = struct.pack('!BBHII',128,111,seq,32000+seq*960,0x11334455)
            audio.rtp.sendto(audio.send.protect(header + encoded), audio.target)
            offset += 1920
            seq += 1
            time.sleep(max(0, start + seq*.020 - time.monotonic()))
        observed = array.array('h')
        deadline = time.monotonic() + 70
        while time.monotonic() < deadline and sdk.poll() is None:
            if not select.select([audio.rtp], [], [], .05)[0]:
                continue
            packet = audio.rtp.recv(8192)
            clear = audio.receive.unprotect(packet)
            require(clear[1]&127 == 111 and not clear[0]&0x10, 'Opus RTP identity')
            observed.frombytes(codec.decode(clear[12 + 4*(clear[0]&15):]))
            require(len(observed) <= 48000*40, 'playout observation bound')
            if len(observed) >= 24000 and max(map(abs, observed)) > 500:
                break
        peak = max(map(abs, observed), default=0)
        save(self.out/'playout.json', dict(codec='Opus', frames_sent=seq,
             decoded_samples=len(observed), peak=peak, source_sha256=clip['sha256']))
        require(len(observed) >= 24000 and peak > 500, 'audible model response on the carrier')
        return seq

    def confirm_bye(self, peer, token, tag, sdk=None):
        require(peer.reader.readline(8192).split() == [b'BYE', b'sips:phone@127.0.0.1:9', b'SIP/2.0'],
                'hangup received on original authenticated carrier connection')
        headers = {}
        while True:
            line = peer.reader.readline(8192)
            if line == b'\r\n':
                break
            require(bool(line) and len(headers) < 64, 'bounded BYE headers')
            key, value = line.split(b':', 1)
            headers[key.lower()] = value.strip()
        require(headers[b'call-id'] == (token+'@peer.test').encode() and
                ('tag='+tag).encode() in headers[b'from'] and
                ('tag='+token).encode() in headers[b'to'], 'same dialog')
        if sdk is not None:
            require(sdk.poll() is None, 'SDK completed before hangup receipt')
        fields = [b'SIP/2.0 200 OK'] + [key+b': '+headers[key] for key in
            (b'via',b'from',b'to',b'call-id',b'cseq')]
        peer.sock.sendall(b'\r\n'.join(fields+[b'Content-Length: 0', b'', b'']))

    def business(self):
        self.snapshot('before')
        self.health(0)
        if self.carrier_calls == 2:
            self.unready_media()
        with contextlib.ExitStack() as stack:
            audio = CarrierAudio()
            audio.setUp()
            stack.callback(audio.doCleanups)
            codec = Codec('OPUS', stack.callback)
            ctx = ssl.create_default_context(cafile=self.ca[0])
            ctx.load_cert_chain(*self.carrier)
            port = self.plan['sip_port']
            raw = stack.enter_context(socket.create_connection(('127.0.0.1',port), timeout=12))
            sock = stack.enter_context(ctx.wrap_socket(raw, server_hostname='server.test'))
            peer = Peer(sock, f'127.0.0.1:{port}')
            stack.callback(peer.reader.close)
            token = secrets.token_hex(8)
            self.budget('calls')
            peer.send('INVITE', token, body=audio.offer('OPUS').encode(),
                      extra=b'Content-Type: application/sdp\r\n')
            tag = peer.response(180, 'INVITE', token)
            notice = self.receive()['event']
            require(notice['type']=='live.transport.incoming' and any(
                h['name'].lower() == 'call-id' and h['value'] == token+'@peer.test'
                for h in notice['data']['sip_headers']), 'signed event for the same dialog')
            self.budget('sdk_requests',6)
            sdk = self.client(notice['data']['session_id'], stack)
            require(peer.response(200, 'INVITE', token) == tag, 'accepted invitation')
            audio.read_answer(peer.body)
            contact = re.search(r'(sips:[^> ]+)',peer.headers['contact'])[1]
            time.sleep(.15)
            require(not (self.out/'sdk-accepted').exists(), 'SDK completed before dialog ACK')
            peer.send('ACK', token, to_tag=tag, branch=token+'ack', target=contact)
            self.budget('sidebands')
            self.wait_file(self.out/'sdk-attached', sdk, 10)
            self.budget('voice_inputs')
            seq = self.speech(audio, codec, sdk)
            if self.plan.get('hangup_tail'):
                # Keep real input packets flowing until the SDK has sent BYE.
                # The backend still has to close its model session and sideband.
                encoded = codec.encode(bytes(1920))
                def send_tail():
                    nonlocal seq
                    header = struct.pack('!BBHII',128,111,seq,32000+seq*960,0x11334455)
                    audio.rtp.sendto(audio.send.protect(header+encoded), audio.target)
                    seq += 1
                for _ in range(5):
                    send_tail()
                    time.sleep(.02)
                save(self.out/'carrier-heard', True)
                start = seq
                while not sock.pending() and not select.select([sock], [], [], .02)[0]:
                    require(seq-start < 200, 'carrier BYE deadline during active input')
                    send_tail()
                send_tail()
                save(self.out/'hangup-tail.json', dict(codec='Opus', frames=seq-start+5,
                     sdk_hangup_during_input=True))
            else:
                save(self.out/'carrier-heard', True)
            self.confirm_bye(peer, token, tag, sdk)
            require(sdk.wait(25) == 0, 'SDK receipt or sideband close failure')
        self.budget('client_results')
        observed = read(self.out/'voice.json')
        require(observed['input'] == self.plan['retained_query']['expected_transcript'] and
                re.sub(r'[^a-z0-9]', '', observed['output'].lower()) == observed['version'] and
                observed['kinds'].count('session.delegation.created') == 1 and
                observed['kinds'].count('session.closed') == 1,
                'model transcription and project lookup quality')
        self.health(self.carrier_calls)
        self.same_model()
        self.snapshot('live-drained')
        self.core('core-after')
        self.snapshot('final-drained')


    def unready_media(self):
        # Freeze only this call's new process; a pidfd pins its identity.
        proc_table = base.lifecycle.guard.processes
        prior = proc_table()
        with contextlib.ExitStack() as stack:
            audio = CarrierAudio()
            audio.setUp()
            stack.callback(audio.doCleanups)
            ctx = ssl.create_default_context(cafile=self.ca[0])
            ctx.load_cert_chain(*self.carrier)
            port = self.plan['sip_port']
            raw = stack.enter_context(socket.create_connection(('127.0.0.1',port), timeout=12))
            sock = stack.enter_context(ctx.wrap_socket(raw, server_hostname='server.test'))
            peer = Peer(sock, f'127.0.0.1:{port}')
            stack.callback(peer.reader.close)
            token = secrets.token_hex(8)
            self.budget('calls')
            peer.send('INVITE', token, body=audio.offer('OPUS').encode(),
                      extra=b'Content-Type: application/sdp\r\n')
            tag = peer.response(180, 'INVITE', token)
            notice = self.receive()['event']
            require(notice['type']=='live.transport.incoming' and any(
                h['name'].lower()=='call-id' and h['value']==token+'@peer.test'
                for h in notice['data']['sip_headers']), 'fault call notice')
            self.budget('sdk_requests')
            sdk = self.client(notice['data']['session_id'], stack, blocked_media=True)
            require(peer.response(200, 'INVITE', token)==tag, 'pending fault call answer')
            expected = ['q4t-sip-media', '3', '127.0.0.1', '127.0.0.1']
            candidates = [(pid,row) for pid,row in proc_table().items() if
                pid not in prior and row.get('ppid')==self.model.pid and row.get('argv')==expected]
            require(len(candidates)==1, 'exact owned media worker identity')
            pid,row = candidates[0]
            handle = os.pidfd_open(pid)
            stack.callback(os.close, handle)
            current = proc_table().get(pid)
            require(current and current['ppid']==self.model.pid and current['argv']==expected
                    and current['start_ticks']==row['start_ticks'], 'pinned media worker identity')
            signals.pidfd_send_signal(handle, signals.SIGSTOP)
            def resume_owned():
                try: signals.pidfd_send_signal(handle, signals.SIGCONT)
                except ProcessLookupError: pass
            stack.callback(resume_owned)
            end = time.monotonic()+1
            while time.monotonic()<end and proc_table().get(pid,{}).get('state')!='T':
                time.sleep(.01)
            require(proc_table().get(pid,{}).get('state')=='T', 'owned media paused')
            contact = re.search(r'(sips:[^> ]+)',peer.headers['contact'])[1]
            peer.send('ACK',token,to_tag=tag,branch=token+'ack',target=contact)
            self.confirm_bye(peer, token, tag)
            require(sdk.wait(8)==0, 'explicit unavailable media HTTP receipt')
            require(select.select([handle],[],[],3)[0], 'unresponsive child terminated')
        self.same_model()
        self.snapshot('unready-drained')


class ActivationStage(Stage):
    carrier_calls = 2
    limits = dict(LIMITS, calls=2, sdk_requests=7)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare','run','finalize'])
    parser.add_argument('--qualification', type=Path)
    parser.add_argument('--hangup-tail', action='store_true')
    parser.add_argument('--unready-media', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    out = args.output.resolve()
    require(out.is_relative_to(ROOT/'.q4t-work'), 'private evidence path')
    if args.action == 'prepare':
        live.prepare(out, args.qualification)
        plan = read(out/'plan.json')
        signal_worker = Path(plan['binary']).with_name('q4t-live-sip')
        audio_worker = signal_worker.with_name('q4t-sip-media')
        limits = ActivationStage.limits if args.unready_media else LIMITS
        plan.update(schema=SCHEMA,limits=limits,unready_media=args.unready_media,
                    hangup_tail=args.hangup_tail,port=18821,operations_port=18822,
                    public_port=18823,sip_port=18824,sip_binary=str(signal_worker),
                    sip_sha256=sha(signal_worker),audio_worker=str(audio_worker),
                    audio_sha256=sha(audio_worker))
        for pattern in ('live_sip*','sip_media*','live_rtp_codec*'):
            for p in (ROOT/'src/service').glob(pattern):
                plan['sources'][str(p.relative_to(ROOT))]=sha(p)
        save(out/'plan.json', plan)
    elif args.action == 'run':
        plan = read(out/'plan.json')
        require(plan['schema']==SCHEMA and sha(plan['audio_worker'])==plan['audio_sha256'],
                'stage identity')
        with base.lifecycle.child_subreaper():
            stage = ActivationStage if plan.get('unready_media') else Stage
            stage(plan).run()
    else:
        plan, result = read(out/'plan.json'), read(out/'result.json')
        limits = ActivationStage.limits if plan.get('unready_media') else LIMITS
        require(result['status']=='PASS' and result['default_unchanged'] and
                result['seconds'] <= limits['seconds'] and all(
                    result['counts'][k]==v for k,v in limits.items() if k not in ('seconds','controls')),
                'bounded business acceptance')
        env = qualify.environment(read(out/'lease/exit.json'), [json.loads(x) for x in
            (out/'lease/nvml-queries.jsonl').read_text().splitlines()], [read(out/'cleanup.json')], expected_starts=1)
        require(all(sha(ROOT/n)==h for n,h in plan['sources'].items()), 'source drift')
        save(out/'acceptance.json', dict(environment=env,seconds=result['seconds'],
            counts=result['counts'],
            blocked_media=read(out/'unready-media.json') if plan.get('unready_media') else None, sdk=read(out/'sdk-receipts.json'), voice=read(out/'voice.json'),
            playout=read(out/'playout.json'), core='42', default_unchanged=True,
            **{k:plan[k] for k in ('binary_sha256','ingress_sha256','sip_sha256','audio_sha256')}))


if __name__ == '__main__':
    main()

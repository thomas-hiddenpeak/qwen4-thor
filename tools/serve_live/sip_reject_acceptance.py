"""Carrier INVITE to signed webhook, scoped official SDK rejection and remote ACK."""
import argparse
import base64
import contextlib
import json
import os
from pathlib import Path
import secrets
import select
import socket
import ssl
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_gateway import tls_fixture
from tools.serve_live import responses_acceptance as live
from tools.serve_live.test_sip_trunk import PEER, Peer
from tools.serve_realtime import transcription_acceptance as base
from tools.serve_recovery import profile_acceptance as qualify
from tools.serve_service.package import read, require, save, sha

LIMITS = dict(starts=1, ingress_starts=1, calls=1, sdk_requests=4, chats=1,
              controls=2000, seconds=300)
SCHEMA = 'q4t-live-sip-reject-v1'


class Stage(base.Stage):
    limits = LIMITS
    subscriber = None

    def receive(self, timeout=8):
        line = self.subscriber.stdout
        require(select.select([line], [], [], timeout)[0], 'subscriber deadline')
        msg = json.loads(line.readline())
        if 'error' in msg:
            save(self.out/'subscriber-error.json', {k:v for k,v in msg.items()
                 if k == 'error' or type(v) is int})
        require('error' not in msg, 'subscriber failure: ' + str(msg.get('error')))
        return msg

    def start(self):
        pki = self.out/'trunk-pki'
        pki.mkdir(mode=0o700)
        self.ca = tls_fixture.ca(pki/'ca')
        now = int(time.time())

        def issue(name, purpose, subject):
            return tls_fixture.issue(pki/name, name, self.ca, now-120, now+3600,
                                     purpose=purpose, host=subject)

        cert, key = issue('listener', 'serverAuth', 'server.test')
        self.carrier = issue('carrier', 'clientAuth', PEER)
        hook_cert, hook_key = issue('subscriber', 'serverAuth', 'localhost')
        listener = pki/'listener'
        pem = listener/'agent.pem'
        fd = os.open(pem, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as out:
            out.write(cert.read_bytes() + key.read_bytes())
        (listener/'cafile.pem').write_bytes(self.ca[0].read_bytes())
        secret = 'whsec_' + base64.b64encode(secrets.token_bytes(32)).decode()
        sdk = Path(self.plan['sdk_directory'])/'node_modules/openai'
        with (self.out/'subscriber.log').open('xb') as log:
            self.subscriber = subprocess.Popen(
                ['node', str(ROOT/'tools/serve_live/sip_webhook_peer.mjs'), str(sdk)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, bufsize=0,
                env={**os.environ, 'NODE_EXTRA_CA_CERTS':self.plan['tls']['certificate']})
        self.history[self.subscriber.pid] = base.lifecycle.guard.processes()[self.subscriber.pid]
        self.dual = self.plan.get('dual', False)
        event_types = ['live.transport.incoming']
        if self.dual:
            event_types.append('realtime.call.incoming')
        api = dict(baseURL=f"https://127.0.0.1:{self.plan['public_port']}/v1",
                   ownerKey=self.keys['alpha'], otherKey=self.keys['beta'], eventType=event_types[-1])
        self.subscriber.stdin.write((json.dumps(dict(cert=str(hook_cert), key=str(hook_key),
            secret=secret, api=api, responses=[204]*len(event_types)))+'\n').encode())
        self.subscriber.stdin.flush()
        port = self.receive()['port']
        network = dict(allow_http=False, public_networks=False, allowed_hosts=['localhost'],
                       allowed_networks=['127.0.0.1/32','::1/128'], ports=[port],
                       timeout_ms=10000, max_redirects=0, ca_file=str(self.ca[0]))
        config = dict(client_id='alpha', listen_ip='127.0.0.1', port=self.plan['sip_port'],
                      certificate_dir=str(listener), peer_subject=PEER,
                      webhook=dict(url=f'https://localhost:{port}/calls', secret=secret,
                                   network=network))
        if self.dual:
            config['webhook']['event_types'] = event_types
        save(self.out/'trunks.json', dict(trunks=[config]))
        require(sha(self.plan['sip_binary']) == self.plan['sip_sha256'], 'SIP binary drift')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', self.plan['sip_port']))
        super().start()

    def extra_model_args(self):
        return ['--sip-trunks-file', str(self.out/'trunks.json')]

    def health(self, delivered):
        until = time.monotonic()+10
        while time.monotonic() < until:
            status, raw = self.http('/v1/config')
            require(status == 200, 'local config unavailable')
            sip = json.loads(raw)['sip']
            require(sip['activation_failures'] == sip['webhook_failures'] == 0,
                    'SIP activation/delivery failed: '+str(sip))
            if sip['configured'] == sip['ready'] == 1 and sip['webhooks_delivered'] == delivered:
                return sip
            time.sleep(.05)
        raise TimeoutError('trunk state or webhook receipt deadline')

    def business(self):
        before = self.health(0)
        self.snapshot('before')
        ctx = ssl.create_default_context(cafile=self.ca[0])
        ctx.load_cert_chain(*self.carrier)
        port = self.plan['sip_port']
        token = secrets.token_hex(8)
        with contextlib.ExitStack() as stack:
            raw = stack.enter_context(socket.create_connection(('127.0.0.1',port), timeout=10))
            sock = stack.enter_context(ctx.wrap_socket(raw, server_hostname='server.test'))
            peer = Peer(sock, f'127.0.0.1:{port}')
            stack.callback(peer.reader.close)
            self.budget('calls')
            self.budget('sdk_requests', 4)
            peer.send('INVITE', token)
            tag = peer.response(180, 'INVITE', token)
            notice = self.receive()
            event = notice['event']
            require(event['type'] == 'live.transport.incoming' and event['data']['type'] == 'sip'
                    and event['data']['session_id'].startswith('live_'), 'authenticated incoming event')
            require(any(h['name'].lower() == 'call-id' and h['value'] == token+'@peer.test'
                        for h in event['data']['sip_headers']), 'webhook refers to this transaction')
            notices = [dict(type=event['type'], id=event['id'], sha256=notice['digest'])]
            if self.dual:
                second = self.receive()
                rtc = second['event']
                require(rtc['type'] == 'realtime.call.incoming' and rtc['id'] != event['id'] and
                        set(rtc['data']) == {'sip_headers','call_id'} and
                        rtc['data']['call_id'].startswith('rtc_') and
                        rtc['data']['sip_headers'] == event['data']['sip_headers'],
                        'independent Realtime handle for same incoming call')
                notices.append(dict(type=rtc['type'], id=rtc['id'], sha256=second['digest']))
            disposition = 603 if self.dual else 486
            if not sock.pending():
                ready, _, _ = select.select([sock, self.subscriber.stdout], [], [], 9)
                if self.subscriber.stdout in ready:
                    self.receive()
                    raise AssertionError('SDK completed before carrier rejection')
            require(peer.response(disposition, 'INVITE', token) == tag, 'rejection transaction')
            require(not select.select([self.subscriber.stdout], [], [], .20)[0],
                    'SDK completed before carrier ACK')
            peer.send('ACK', token, to_tag=tag)
            controls = self.receive()['control']
            require(controls == dict(foreign=404, invalid=400, owner=200, empty=True, retired=404),
                    'public SDK disposition')
        after = self.health(len(notices))
        self.same_model()
        self.core('core-after')
        self.snapshot('final-drained')
        save(self.out/'carrier.json', dict(sip=[180,disposition,'ACK'], controls=controls,
            webhooks=notices, core='42',
            before=before, after=after))

    def cleanup(self):
        try:
            if self.subscriber:
                self.subscriber.terminate()
                try:
                    self.subscriber.wait(3)
                except subprocess.TimeoutExpired:
                    self.subscriber.kill()
                    self.subscriber.wait(3)
                finally:
                    self.subscriber.stdin.close()
                    self.subscriber.stdout.close()
        finally:
            super().cleanup()
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('127.0.0.1', self.plan['sip_port']))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare','run','finalize'])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--qualification', type=Path)
    parser.add_argument('--dual', action='store_true', help='prepare both event types and reject via Realtime')
    args = parser.parse_args()
    out = args.output.resolve()
    require(out.is_relative_to(ROOT/'.q4t-work'), 'private evidence path')
    if args.action == 'prepare':
        live.prepare(out, args.qualification)
        plan = read(out/'plan.json')
        sip = Path(plan['binary']).with_name('q4t-live-sip')
        plan.update(schema=SCHEMA, limits=LIMITS, dual=args.dual, port=18811, operations_port=18812,
                    public_port=18813, sip_port=18814, sip_binary=str(sip), sip_sha256=sha(sip))
        for path in sorted((ROOT/'src/service').glob('live_sip*')):
            plan['sources'][str(path.relative_to(ROOT))] = sha(path)
        save(out/'plan.json', plan)
    elif args.action == 'run':
        plan = read(out/'plan.json')
        require(plan['schema'] == SCHEMA, 'plan identity')
        with base.lifecycle.child_subreaper():
            Stage(plan).run()
    else:
        result, plan = read(out/'result.json'), read(out/'plan.json')
        require(result['status'] == 'PASS' and result['default_unchanged'] and
                result['seconds'] <= LIMITS['seconds'] and
                all(result['counts'][k] == v for k,v in LIMITS.items()
                    if k not in ('seconds','controls')), 'bounded business run')
        env = qualify.environment(read(out/'lease/exit.json'), [
            json.loads(s) for s in (out/'lease/nvml-queries.jsonl').read_text().splitlines()],
            [read(out/'cleanup.json')], expected_starts=1)
        require(all(sha(ROOT/n) == h for n,h in plan['sources'].items()), 'qualified sources')
        save(out/'acceptance.json', dict(environment=env, business=read(out/'carrier.json'),
            seconds=result['seconds'], counts=result['counts'], default_unchanged=True,
            engine_sha256=plan['binary_sha256'], ingress_sha256=plan['ingress_sha256'],
            sip_sha256=plan['sip_sha256']))


if __name__ == '__main__':
    main()

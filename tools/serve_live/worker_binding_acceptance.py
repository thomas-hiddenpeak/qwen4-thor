"""Check a changed media ELF, core Chat and restoration on the same engine."""
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_live import client_secret_rtc_acceptance as ice
from tools.serve_live import media_create_acceptance as media
from tools.serve_service import package as pkg


class Stage(ice.Stage):
    limits = dict(starts=1, ingress_starts=1, sdk_posts=2, chats=1,
                  controls=2000, seconds=300)

    @staticmethod
    def prepare_fixture(out, plan):
        ice.Stage.prepare_fixture(out, plan)
        installed = Path(os.environ['Q4T_INSTALLED_LIVE_WORKERS']).resolve(strict=True)
        artifacts = pkg.read(ROOT/'docs/evidence/serve-live-worker-install-20261123/artifacts.json')
        files = {n:artifacts[n]['sha256'] for n in pkg.LIVE_WORKERS}
        directory = out/'engine'
        directory.mkdir(mode=0o700)
        binary = directory/'q4t'
        shutil.copy2(plan['binary'], binary)
        for name, digest in files.items():
            image = pkg.read_live_worker(installed/name, digest)
            (directory/name).write_bytes(image)
            (directory/name).chmod(0o500)
        plan.update(binary=str(binary), installed=str(installed),
                    live_workers=dict(profile='q4t-live-workers-v1',
                                      engine_sha256=plan['binary_sha256'], files=files))

    def extra_model_args(self):
        return super().extra_model_args() + pkg.live_transport_args(self.plan)

    def business(self):
        media.rtc.Gst.init(None)
        binary = Path(self.plan['binary']).with_name('q4t-live-webrtc')
        self.snapshot('before')
        # A valid ELF with trailing bytes still loads on Linux. Only its
        # identity changed; a successful negotiation here would be a failure.
        binary.chmod(0o700)
        with binary.open('ab') as stream:
            stream.write(b'unqualified')
        binary.chmod(0o500)
        config = dict(model='qwen3.8-flash-next',
                      client=dict(data_channel=dict(allowed_client_events=['session.close'])))
        with media.rtc.peer_offer('OPUS') as (peer, _, _, channel, messages, offer, _):
            denied = self.create(offer, config)
            pkg.require(denied == dict(status=503, code='live_media_unavailable'),
                        'changed media identity: '+repr(denied))
            self.core('core-while-media-unavailable')
            replacement = binary.with_suffix('.new')
            digest = self.plan['live_workers']['files'][binary.name]
            replacement.write_bytes(pkg.read_live_worker(Path(self.plan['installed'])/binary.name, digest))
            replacement.chmod(0o500)
            replacement.replace(binary)
            reply = self.create(offer, config, client='beta')
            pkg.require(reply['status'] == 201, 'restored SDK create: '+str(reply.get('code')))
            media.rtc.accept_answer(peer, reply['data']['transport']['sdp'])
            def event(kind):
                return next((x for wire in list(messages) if (x := json.loads(wire))['type'] == kind), None)
            started = media.rtc.wait_for(lambda: event('session.started'), 'session.started', 15)
            pkg.require(started['session']['id'] == reply['data']['session']['id'],
                        'restored WebRTC session identity')
            channel.send_string(json.dumps(dict(type='session.close', event_id='finish')))
            media.rtc.wait_for(lambda: event('session.closed'), 'session.closed', 15)
        self.same_model()
        self.snapshot('final-drained')
        pkg.save(self.out/'observation.json', dict(corrupt=denied, restored=201,
            data_channel=['session.started','session.closed'], same_model=True,
            workers=self.plan['live_workers']))


if __name__ == '__main__':
    media.main(Stage, 'q4t-live-managed-worker-recovery-v1')

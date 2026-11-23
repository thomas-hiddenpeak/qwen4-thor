"""One bounded model generation serving signed Realtime credentials over TLS."""
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_live import media_create_acceptance as media
from tools.serve_live.client_acceptance import phone_query_matches, retained_query
from tools.serve_service.package import require, save


class Stage(media.Stage):
    limits = dict(starts=1, ingress_starts=1, sdk_workflows=1, chats=1,
                  controls=2000, seconds=220)

    def business(self):
        self.snapshot('before')
        self.budget('sdk_workflows')
        clip = retained_query()
        args = dict(key=self.keys['alpha'], base_url=f'https://127.0.0.1:{self.plan["public_port"]}/v1',
                    pcm=str(clip['path']))
        actor = subprocess.run(['node', ROOT/'tools/serve_live/client_secret_sdk.mjs'],
            input=json.dumps(args), text=True, capture_output=True, timeout=100,
            env=dict(os.environ, NODE_EXTRA_CA_CERTS=self.plan['tls']['certificate'],
                     Q4T_NODE_SDK_DIR=self.plan['sdk_directory']))
        require(actor.returncode == 0, 'Realtime credential SDK: '+actor.stderr[-400:])
        observation = json.loads(actor.stdout)
        save(self.out/'observation.json', observation)
        require(phone_query_matches(observation['transcript'], clip['expected_transcript']),
                'transcription of retained project query')
        self.same_model()
        self.snapshot('realtime-drained')
        self.core('core-after')
        self.snapshot('final-drained')


if __name__ == '__main__':
    media.main(Stage, 'q4t-realtime-client-secret-business-v1')

"""Real SDK/native TLS against a synthetic CPU peer; no model qualification."""
import json
import os
from pathlib import Path
import select
import subprocess
import unittest

from tools.serve_realtime import test_history_sdk as history
from tools.serve_realtime.history_acceptance import settings
from tools.serve_realtime.ingress_fixture import NativeIngress

ROOT = Path(__file__).resolve().parents[2]


class RecoverySdk(unittest.TestCase):
    setUpClass = classmethod(history.HistorySdk.setUpClass.__func__)
    setUp = history.HistorySdk.setUp
    close = history.HistorySdk.close

    def start(self, mode, *, secure=True):
        scheme='https' if secure else 'http'
        args = dict(base_url=f'{scheme}://127.0.0.1:{self.front.port}/v1', api_key='A'*40,
            ca_file=self.tls['certificate'] if secure else None, file=str(self.audio), mode=mode,
            settings=settings('near_stream' if mode == 'hold_stream' else 'manual'))
        env = {k:v for k,v in os.environ.items() if k not in
               ('NODE_OPTIONS','NODE_PATH','LD_PRELOAD')}
        process = subprocess.Popen(['node',str(ROOT/'tools/serve_realtime/recovery_sdk.mjs')],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        def cleanup():
            if process.poll() is None:process.kill()
            process.communicate(timeout=3)
        self.addCleanup(cleanup)
        process.stdin.write(json.dumps(args).encode());process.stdin.close();process.stdin=None
        return process

    def finish(self, process):
        stdout,stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode,0,stderr.decode()+stdout.decode())
        value = json.loads(stdout);self.assertEqual(value['status'],'PASS',value)
        self.assertEqual(value['connections'],1);self.assertFalse(value['automatic_replay'])
        return value

    def ready(self, process, mode):
        self.assertTrue(select.select([process.stdout],[],[],10)[0], 'no bounded ready receipt')
        value = json.loads(process.stdout.readline())
        self.assertEqual(value['phase'],'ready',value);self.assertEqual(value['mode'],mode)
        return value

    def test_one_complete_connection_has_actual_input_and_normal_close(self):
        value = self.finish(self.start('complete'))
        self.assertEqual(value['close_code'],1000)
        self.assertEqual(value['transport'],'openai-realtime-wss')
        self.assertEqual(self.received,[self.audio.read_bytes()])
        self.assertEqual(value['turn']['terminal']['transcript'],'CPU fixture only')

    def test_explicit_loopback_plaintext_mode_does_not_require_tls(self):
        old=self.front;old.close()
        self.front=NativeIngress(self.out/'plain',Path(os.environ['Q4T_INGRESS']),
            old.port,self.server.server_port,None,'e'*64,{'alpha':'A'*40},
            None,lambda:True)
        value=self.finish(self.start('complete',secure=False))
        self.assertEqual(value['transport'],'ws-loopback')
        self.assertEqual(value['close_code'],1000)
        self.assertEqual(self.received,[self.audio.read_bytes()])
        self.expect_abort=True;process=self.start('hold_complete',secure=False)
        self.ready(process,'hold_complete')
        self.front.process.kill();self.front.process.wait(5)
        value=self.finish(process)
        self.assertEqual(value['transport'],'ws-loopback')
        self.assertEqual(value['close_code'],1006)
        self.assertEqual(self.received,[self.audio.read_bytes()]*2)

    def test_interrupted_stream_does_not_commit_or_retry_withheld_input(self):
        self.expect_abort=True;process=self.start('hold_stream')
        ready=self.ready(process,'hold_stream')
        self.assertEqual(ready['sent_audio_bytes'],24000)
        self.assertLess(ready['sent_audio_bytes'],ready['total_audio_bytes'])
        self.front.process.kill();self.front.process.wait(5)
        value=self.finish(process)
        self.assertEqual(value['close_code'],1006)
        self.assertIsNone(value['turn']);self.assertEqual(value['partial']['delta'],'CPU fixture')
        self.assertEqual(self.received,[])
        self.assertNotIn('input_audio_buffer.commit',self.commands)

    def test_payload_connection_loss_needs_a_separate_explicit_new_connection(self):
        self.expect_abort=True;process=self.start('hold_complete')
        ready=self.ready(process,'hold_complete')
        self.assertEqual(ready['sent_audio_bytes'],len(self.audio.read_bytes()))
        old=self.front;old.process.kill();old.process.wait(5)
        value=self.finish(process);self.assertEqual(value['close_code'],1006)
        old.close()
        self.front=NativeIngress(self.out/'recovered',Path(os.environ['Q4T_INGRESS']),
            old.port,self.server.server_port,None,'e'*64,{'alpha':'A'*40},
            self.tls,lambda:True)
        after=self.finish(self.start('complete'))
        self.assertEqual(after['close_code'],1000)
        self.assertEqual(self.received,[self.audio.read_bytes()]*2)
        self.assertEqual(self.commands.count('input_audio_buffer.commit'),2)


if __name__ == '__main__':
    unittest.main()

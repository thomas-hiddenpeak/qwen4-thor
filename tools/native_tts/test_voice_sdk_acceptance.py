"""CPU negative checks for SDK evidence; does not qualify service behavior."""
from contextlib import redirect_stdout
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from tools.native_tts import voice_sdk_acceptance as driver


class VoiceSdkDriver(unittest.TestCase):
    def setUp(self):
        root=driver.ROOT/'.q4t-work/voice-sdk-driver-cpu';root.mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=root);self.addCleanup(self.temp.cleanup)
        self.stage=driver.Stage.__new__(driver.Stage)
        self.stage.out=Path(self.temp.name);self.stage.started=time.monotonic()
        self.stage.counts=dict(posts=0,controls=0,starts=0,ingress_starts=0)
        self.stage.phase=dict(edge_port=18490,sdk=dict(python='/cpu-only'),
                             tls_inputs=dict(ca=dict(path='/cpu-only-ca')))
        self.stage.artifacts=mock.Mock();self.stage.observe=mock.Mock()
        self.raw=json.dumps(dict(error=dict(message='CPU',type='invalid_request_error',
                                            code='object_not_found'))).encode()
        self.receipt=dict(ok=False,version=driver.voice_sdk.VERSION,
            wire_bytes=len(self.raw),wire_sha256=hashlib.sha256(self.raw).hexdigest(),
            exchanges=[dict(url='https://127.0.0.1:18490/voice-test/v1/audio/voices',
                            status=404,method='POST')])

    def invoke(self,receipt):
        def run(*args,**kwargs):
            config=json.loads(kwargs['input']);Path(config['output']).write_bytes(self.raw)
            return SimpleNamespace(returncode=1,stdout=json.dumps(receipt),stderr='')
        with mock.patch.object(driver.subprocess,'run',side_effect=run),redirect_stdout(io.StringIO()):
            return self.stage.sdk('negative','voice_create',dict(consent='cons_actual'),status=404)

    def test_negative_wire_success_and_retry_cannot_forge_acceptance(self):
        raw,_=self.invoke(self.receipt);self.assertEqual(raw,self.raw)
        for mutate in ('ok','status','duplicate','hash'):
            value=copy.deepcopy(self.receipt)
            if mutate=='ok':value['ok']=True
            elif mutate=='status':value['exchanges'][0]['status']=200
            elif mutate=='duplicate':value['exchanges']*=2
            else:value['wire_sha256']='0'*64
            with self.assertRaises(RuntimeError):self.invoke(value)
        self.assertEqual(self.stage.observe.call_count,1)

    def test_bounds_precede_next_attempt_and_include_entry_restarts(self):
        for kind in ('posts','starts','ingress_starts'):
            self.stage.counts[kind]=driver.LIMITS[kind]
            with self.assertRaisesRegex(RuntimeError,'attempt bound'):
                self.stage.budget(kind)
            self.assertEqual(self.stage.counts[kind],driver.LIMITS[kind])
        self.stage.started=time.monotonic()-driver.LIMITS['seconds']
        with self.assertRaisesRegex(RuntimeError,'deadline'):self.stage.budget('controls')
        self.assertEqual(self.stage.counts['controls'],0)

    def test_entry_cleanup_failure_keeps_model_cleanup_and_failed_result(self):
        self.stage.plan=dict(defaults={})
        self.stage.start=mock.Mock();self.stage.business=mock.Mock()
        self.stage.cleanup=mock.Mock()
        self.stage.edge=SimpleNamespace(close=mock.Mock(side_effect=RuntimeError('owned entry cleanup')))
        with mock.patch.object(driver.direct.chat,'probe_ports'), \
                mock.patch.object(driver.direct,'defaults',return_value={}):
            with self.assertRaisesRegex(RuntimeError,'owned entry cleanup'):
                self.stage.run()
        self.stage.cleanup.assert_called_once()
        result=json.loads((self.stage.out/'result.json').read_text())
        self.assertEqual(result['status'],'FAILED')
        self.assertIn('owned entry cleanup',result['entry_cleanup_error'])


if __name__=='__main__':unittest.main()

"""CPU-only observation failure while the owned HTTP client is blocked."""
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
sys.path.insert(0,str(HERE.parent/'mtp_suffix_nsys'))
import run_acceptance as http
import nvlog_stream as codec

SPEC=importlib.util.spec_from_file_location('checked_client_stage',
    HERE.parent/'mtp_suffix_nsys/gpu_pair_stage.py')
stage=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(stage)


class CheckedClientContracts(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.base=Path(temporary.name)

    def test_normal_exit_checks_before_and_after_without_signals(self):
        child=MagicMock(pid=7,returncode=0);child.wait.side_effect=[subprocess.TimeoutExpired('client',.1),0]
        child.poll.return_value=0;check=MagicMock()
        with patch.object(http.subprocess,'Popen',return_value=child):
            code=http.run_checked_client(['client'],{},io.StringIO(),check,self.base/'receipt.json')
        self.assertEqual(code,0);self.assertEqual(check.call_count,4)
        child.terminate.assert_not_called();child.kill.assert_not_called()
        self.assertEqual(json.loads((self.base/'receipt.json').read_text())['cleanup_errors'],[])

    def test_failure_before_spawn_never_starts_client(self):
        primary=ValueError('collector already exited')
        with patch.object(http.subprocess,'Popen') as spawn:
            with self.assertRaises(ValueError) as caught:
                http.run_checked_client(['client'],{},io.StringIO(),MagicMock(side_effect=primary),self.base/'receipt.json')
        self.assertIs(caught.exception,primary);spawn.assert_not_called()
        self.assertIsNone(json.loads((self.base/'receipt.json').read_text())['pid'])

    def test_original_failure_survives_cleanup_and_receipt_errors(self):
        for mode in ('term','kill','cleanup_error','receipt_error'):
            with self.subTest(mode=mode):
                primary=ValueError('native cap exceeded');check=MagicMock(side_effect=[None,primary])
                child=MagicMock(pid=7,returncode=-15);child.poll.return_value=None
                if mode=='kill':child.wait.side_effect=[subprocess.TimeoutExpired('client',2),-9]
                if mode=='cleanup_error':child.terminate.side_effect=OSError('cleanup failed')
                save=http.save if mode!='receipt_error' else MagicMock(side_effect=OSError('disk failed'))
                with patch.object(http.subprocess,'Popen',return_value=child),patch.object(http,'save',side_effect=save):
                    with self.assertRaises(ValueError) as caught:
                        http.run_checked_client(['client'],{},io.StringIO(),check,self.base/(mode+'.json'))
                self.assertIs(caught.exception,primary);self.assertEqual(check.call_count,2)
                child.terminate.assert_called_once()
                self.assertEqual(child.kill.call_count,int(mode=='kill'))
                if mode=='receipt_error':self.assertTrue(primary.__notes__)
                else:
                    receipt=json.loads((self.base/(mode+'.json')).read_text())
                    self.assertEqual(bool(receipt['cleanup_errors']),mode=='cleanup_error')
                    self.assertEqual(receipt['failure'],repr(primary))

    def test_native_cap_failure_interrupts_only_owned_waiting_client(self):
        binary=os.environ.get('Q4T_NVLOG_STREAM_BINARY')
        self.assertTrue(binary,'frozen native worker required')
        collector=codec.Collector(binary,self.base/'stream',compressed_limit=64,timeout_seconds=10).start()
        self.addCleanup(collector.abort)
        ready=self.base/'ready'
        command=[sys.executable,'-B','-c',
                 'import pathlib,sys,time;pathlib.Path(sys.argv[1]).write_text("ready");time.sleep(10)',str(ready)]
        writer=None;write_errors=[]
        guard=stage.MeasurementGuard(self.base,time.monotonic()+5)
        guard.extra_check=collector.check
        def produce():
            try:collector.write_chunks([os.urandom(codec.BLOCK)])
            except BaseException as error:write_errors.append(repr(error))
        def check():
            nonlocal writer
            if ready.exists() and writer is None:
                writer=threading.Thread(target=produce);writer.start()
            guard.check()
        bystander=subprocess.Popen([sys.executable,'-B','-c','import time;time.sleep(10)'])
        started=time.monotonic()
        try:
            with (self.base/'client.log').open('w') as log:
                with self.assertRaises(ValueError):
                    http.run_checked_client(command,dict(os.environ),log,check,self.base/'client-process.json')
            self.assertLess(time.monotonic()-started,3)
            self.assertIsNone(bystander.poll())
            self.assertTrue(guard.failure['environment_abort'])
            self.assertFalse(guard.failure['repaired_by_cleanup'])
            receipt=json.loads(collector.receipt_path.read_text())
            self.assertEqual(receipt['error'],'compressed byte cap exceeded')
            self.assertFalse(receipt['frame_completed'])
            client=json.loads((self.base/'client-process.json').read_text())
            self.assertEqual(client['signals'],['TERM']);self.assertFalse(client['cleanup_errors'])
            self.assertEqual(client['returncode'],-15)
        finally:
            collector.abort()
            if writer is not None:writer.join(timeout=2);self.assertFalse(writer.is_alive())
            bystander.terminate();bystander.wait(timeout=2)

    def test_admission_propagates_check_and_keeps_default_call_contract(self):
        for enabled in (False,True):
            output=self.base/str(enabled);output.mkdir()
            fixture=output/'input.jsonl';fixture.write_text('prompt\n')
            item=dict(id='case',path=str(fixture),repeats=1,input_min=1,input_max=1,
                output_tokens=2,prompt_sha256='p',input_tokens=1,role='development_diagnostic')
            value=dict(port=8764,model=str(output));step=dict(kind='timing',head='subset',suffix=True,cases=[item])
            check=MagicMock();row=dict(prompt_sha256='p',actual_input=1,actual_output=2,text='ok',ttft=1,latency=2)
            with patch.object(stage.old.admission.http_runner,'run_case',return_value=[row]) as run, \
                    patch.object(stage.old.admission.http_runner,'event'):
                stage.old.admission.run_cases(value,step,output,None,{},None,client_check=check if enabled else None)
            self.assertEqual(run.call_args.kwargs,{'process_check':check} if enabled else {})

    def test_post_exit_observer_failure_remains_failure(self):
        primary=ValueError('collector failed at client exit')
        child=MagicMock(pid=7,returncode=0);child.wait.return_value=0;child.poll.return_value=0
        check=MagicMock(side_effect=[None,None,primary])
        with patch.object(http.subprocess,'Popen',return_value=child):
            with self.assertRaises(ValueError) as caught:
                http.run_checked_client(['client'],{},io.StringIO(),check,self.base/'receipt.json')
        self.assertIs(caught.exception,primary);child.terminate.assert_not_called()
        self.assertEqual(json.loads((self.base/'receipt.json').read_text())['returncode'],0)


if __name__=='__main__':unittest.main()

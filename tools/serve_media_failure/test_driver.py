"""Offline guardrails: exact budget, immutable input and terminal rejection."""
import copy
import io
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from tools.serve_media_failure import verify as v, fixtures
from tools.serve_protocol.check_contract import load_validators


class Contracts(unittest.TestCase):
    def test_fixed_attempt_scope(self):
        self.assertEqual(len(v.SCENARIOS), v.stage.PLANNED['starts'])
        self.assertEqual(len({r[0] for r in v.SCENARIOS}), 9)
        self.assertEqual(v.stage.PLANNED, dict(generations=5+3+2+1+1+2+2+0+1,
                                              negatives=8, starts=9))
        self.assertTrue(all(v.stage.PLANNED[k] <= v.stage.LIMITS[k] for k in v.stage.PLANNED))

    def test_environment_preserves_no_unrelated_fault(self):
        with patch.dict(os.environ, {'LD_PRELOAD': 'bad', 'Q4T_CLEANUP_FAULT_API': 'wrong',
                                     'Q4T_SERVE_FAULT': 'wrong'}):
            clean = v.environment()
            self.assertNotIn('LD_PRELOAD', clean)
            self.assertFalse(any(k.startswith('Q4T_') for k in clean))
            host = v.environment('host', 'vision.after_forward')
            self.assertEqual(host['Q4T_SERVE_REQUEST'], v.FAULT_REQUEST)
            self.assertNotIn('Q4T_SERVE_DEVICE_FAULT', host)
            startup = v.environment('device', 'startup.vision_load', 'other')
            self.assertNotIn('Q4T_SERVE_REQUEST', startup)
            self.assertNotIn('Q4T_SERVE_FAULT', startup)

    def test_media_fixture_contract_does_not_change_wire(self):
        with tempfile.TemporaryDirectory(dir=v.ROOT/'.q4t-work') as directory:
            folder = Path(directory)/'fixtures'; fixtures.prepare(folder)
            contract = v.FixtureRequestContract(load_validators()['chat'])
            for video in (False, True):
                body = v.media_body(folder, video=video)
                original = copy.deepcopy(body)
                contract.validate(body)
                self.assertEqual(body, original)
                body['messages'][0]['role'] = 'system'
                with self.assertRaises(RuntimeError): contract.validate(body)
            body = v.media_body(folder)
            body['messages'][0]['content'][0]['extra'] = 1
            with self.assertRaises(RuntimeError): contract.validate(body)

    def test_complete_error_wire_must_have_one_done_without_success(self):
        error = b'data: {"error":{"code":"host_memory_exhausted"}}\r\n\r\n'
        done = b'data: [DONE]\r\n\r\n'
        v.prior.error_terminal(error+done, True, 'host_memory_exhausted')
        for raw in (error, error+done+done, error+error+done,
                    b'data: {"choices":[{"finish_reason":"stop"}]}\r\n\r\n'+error+done):
            with self.assertRaises(RuntimeError):
                v.prior.error_terminal(raw, True, 'host_memory_exhausted')

    def test_capture_reads_the_tail_after_error(self):
        payload = b'data: {"error":{}}\n\ndata: [DONE]\n\n'
        sink = io.BytesIO()
        self.assertEqual(v.capture_response(io.BytesIO(payload), sink.write), payload)
        self.assertEqual(sink.getvalue(), payload)
        with self.assertRaises(RuntimeError):
            v.capture_response(io.BytesIO(payload), lambda _: None, maximum=8)

    def test_budget_refuses_without_launching_extra_work(self):
        with tempfile.TemporaryDirectory(dir=v.ROOT/'.q4t-work') as directory:
            run = object.__new__(v.Run)
            run.out, run.deadline = Path(directory), time.time()+30
            run.counts = {k: v.stage.LIMITS[k] for k in v.stage.PLANNED}
            run.counts['starts'] -= 1
            run.budget('starts')
            with self.assertRaises(RuntimeError): run.budget('starts')
            self.assertEqual(len(list(run.out.glob('attempt*'))), 1)
            run.deadline = 0
            with self.assertRaises(RuntimeError): run.budget()


if __name__ == '__main__': unittest.main()

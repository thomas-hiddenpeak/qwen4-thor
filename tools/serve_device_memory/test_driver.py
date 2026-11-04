"""Offline guardrails for bounded fault acceptance; no GPU or HTTP started."""
import copy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from tools.serve_device_memory import verify as v


class Contracts(unittest.TestCase):
    def wire(self, values):
        return ''.join('data: '+json.dumps(x)+'\r\n\r\n' for x in values).encode()+b'data: [DONE]\r\n\r\n'

    def chunk(self, text='710003'):
        return dict(object='chat.completion.chunk',usage=None,
                    choices=[dict(delta=dict(content=text),finish_reason=None)])

    def error(self):
        return dict(error=dict(code='gpu_unhealthy'))

    def test_partial_content_can_only_end_with_error(self):
        values=[self.chunk(),self.error()]
        self.assertEqual(v.error_terminal(self.wire(values),True,'gpu_unhealthy'),values)
        for change in ('finish','usage','duplicate','after-error','missing-done'):
            bad=copy.deepcopy(values)
            if change=='finish':bad[0]['choices'][0]['finish_reason']='stop'
            if change=='usage':bad[0]['usage']={'total_tokens':3}
            if change=='duplicate':bad.append(self.error())
            if change=='after-error':bad.append(self.chunk())
            wire=self.wire(bad)
            if change=='missing-done':wire=wire.replace(b'data: [DONE]',b'')
            with self.subTest(change=change),self.assertRaises((RuntimeError,ValueError)):
                v.error_terminal(wire,True,'gpu_unhealthy')

    def test_json_never_accepts_success_or_wrong_reason(self):
        v.error_terminal(json.dumps(self.error()).encode(),False,'gpu_unhealthy')
        for value in ({'choices':[]}, {'error':{'code':'service_unavailable'}},
                      {'error':{'code':'gpu_unhealthy'},'usage':{}}):
            with self.assertRaises(RuntimeError):v.error_terminal(json.dumps(value).encode(),False,'gpu_unhealthy')

    def test_environment_does_not_leak_other_faults(self):
        with patch.dict(os.environ,{'Q4T_SERVE_FAULT':'unrelated','LD_PRELOAD':'bad','Q4T_KEEP':'no'}):
            env=v.fault_environment(None,None)
            self.assertFalse(any(k.startswith('Q4T_') for k in env));self.assertNotIn('LD_PRELOAD',env)
            self.assertEqual(v.fault_environment('request.prompt_trunk','oom')['Q4T_SERVE_REQUEST'],v.FAULT_REQUEST)
            self.assertNotIn('Q4T_SERVE_REQUEST',v.fault_environment('startup.scheduler_tokens','other'))

    def test_frozen_fixture_schema_and_recovery_cache_threshold(self):
        schemas=v.base.load_validators()
        for body in (v.cache_body(),v.fault_body(),v.fault_body(stream=True,session=True)):
            schemas['chat'].validate(body)
        self.assertEqual(v.cache_body()['messages'][-1]['content'].count('记录仅供占位。'),1400)
        self.assertEqual(v.cache_body()['thinking_token_budget'],8)
        self.assertEqual(len(v.SCENARIOS),v.stage.PLANNED['starts'])
        self.assertEqual(sum(row[-1]==1 for row in v.SCENARIOS),6)
        self.assertEqual(v.stage.PLANNED,dict(starts=9,generations=16,negatives=7))

    def test_attempt_limit_rejects_before_extra_work(self):
        with tempfile.TemporaryDirectory(dir=v.ROOT/'.q4t-work') as folder:
            run=object.__new__(v.Run)
            run.deadline=time.time()+30;run.out=Path(folder)
            run.counts=dict(v.stage.PLANNED);run.counts['starts']-=1
            run.budget('starts')
            with self.assertRaises(RuntimeError):run.budget('starts')
            self.assertEqual(len(list(run.out.glob('attempt*'))),1)
            run.deadline=0
            with self.assertRaises(RuntimeError):run.budget()

    def test_monitor_error_remains_environment_abort(self):
        class Monitor:
            def close(self):raise v.observe.ObservationAbort('lost observation')
        run=object.__new__(v.Run);run.monitor=Monitor()
        with self.assertRaises(v.observe.ObservationAbort):run.stop_monitor()
        self.assertIsNone(run.monitor)


if __name__=='__main__':unittest.main()

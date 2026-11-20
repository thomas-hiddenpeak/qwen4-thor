"""Driver boundaries; no GPU, real model or production service is used."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.serve_realtime import tracing_acceptance as t


class Driver(unittest.TestCase):
    def test_valid_trace_constructor_uses_its_own_scope(self):
        plan=dict(output='/owned/cpu-fixture',limits=t.LIMITS)
        def initialize(owner,actual):
            self.assertIs(actual,plan)
            owner.out=Path(actual['output']);owner.counts=dict(starts=0,chats=0,controls=0)
        with mock.patch.object(t,'check_plan') as own, mock.patch.object(
                t.encrypted.Stage,'__init__',initialize),mock.patch.object(
                t.previous,'check_plan',side_effect=AssertionError('old MCP format gate')):
            stage=t.Stage(plan)
            own.assert_called_once_with(plan)
        self.assertEqual(stage.counts,dict(starts=0,chats=0,controls=0,websocket_sessions=0,ingress_starts=0))
        self.assertIsNone(stage.peer);self.assertIsNone(stage.edge)
        self.assertEqual(stage.observed,{})

    def test_frozen_work_and_no_full_matrix(self):
        self.assertEqual(t.LIMITS,dict(starts=2,chats=6,controls=1200,seconds=900,
            websocket_sessions=2,ingress_starts=2))
        self.assertEqual(t.Stage.request_templates(),{})

    def test_repairs_only_exact_owned_storage_fixture(self):
        with tempfile.TemporaryDirectory(dir=os.environ['Q4T_TEST_OUTPUT_DIR']) as directory:
            path=Path(directory)/'traces';path.write_bytes(t.STORAGE_MARKER)
            t.repair_storage(path);self.assertTrue(path.is_dir())
            with self.assertRaises(RuntimeError):t.repair_storage(path)

    def test_foreign_content_and_symlink_cannot_be_repaired(self):
        with tempfile.TemporaryDirectory(dir=os.environ['Q4T_TEST_OUTPUT_DIR']) as directory:
            source=Path(directory)/'source';source.write_bytes(b'foreign')
            with self.assertRaises(RuntimeError):t.repair_storage(source)
            link=Path(directory)/'link';source.write_bytes(t.STORAGE_MARKER);link.symlink_to(source)
            with self.assertRaises(RuntimeError):t.repair_storage(link)
            self.assertEqual(source.read_bytes(),t.STORAGE_MARKER)

    def test_worker_requires_original_model_and_direct_ancestry(self):
        records={1:dict(start_ticks=10),2:dict(ppid=1,start_ticks=11,argv=['q4t-traces'])}
        self.assertEqual(t.trace_worker(records,1,dict(start_ticks=10))[0],2)
        for key,value in [('ppid',3),('start_ticks',9),('argv',['q4t','serve'])]:
            bad={1:records[1],2:dict(records[2],**{key:value})}
            with self.assertRaises(RuntimeError):t.trace_worker(bad,1,dict(start_ticks=10))
        with self.assertRaises(RuntimeError):t.trace_worker(records,1,dict(start_ticks=9))

    def test_multiple_or_unowned_workers_are_not_signalled(self):
        records={1:dict(start_ticks=10),2:dict(ppid=1,start_ticks=11,argv=['q4t-traces'])}
        with self.assertRaises(RuntimeError):
            t.trace_worker(records|{3:dict(records[2])},1,dict(start_ticks=10))
        with self.assertRaises(RuntimeError):t.trace_worker({1:records[1]},1,dict(start_ticks=10))

    def test_frozen_scope_rejected_before_reading_build(self):
        with mock.patch.object(t,'read',side_effect=AssertionError('must reject before I/O')):
            for changed in (dict(starts=3),dict(chats=7),dict(seconds=901)):
                plan=dict(format=t.FORMAT,limits=dict(t.LIMITS,**changed),requests={},
                    automatic_retries=False,planned={k:v for k,v in t.LIMITS.items()
                    if k not in ('seconds','controls')})
                with self.assertRaises(RuntimeError):t.check_plan(plan)

    def test_fault_signals_only_bound_worker_and_records_healthy_model(self):
        with tempfile.TemporaryDirectory(dir=os.environ['Q4T_TEST_OUTPUT_DIR']) as directory:
            stage=object.__new__(t.Stage);stage.profile_name='light';stage.out=Path(directory)
            stage.model=mock.Mock(pid=100);stage.model_identity=dict(start_ticks=10)
            stage.history={};stage.faults=[];stage.same_model=mock.Mock()
            stage.http=mock.Mock(side_effect=[(200,b'{"realtime_trace_health":{}}'),(200,b'')])
            records={100:dict(start_ticks=10),101:dict(ppid=100,start_ticks=11,argv=['q4t-traces'])}
            with mock.patch.object(t.guard,'processes',return_value=records),mock.patch.object(
                    t.guard,'signal_identity') as send:
                stage.fault('kill-trace-worker',stage.out)
                send.assert_called_once_with(101,records[101],t.signal.SIGKILL)
            row=json.loads((stage.out/'faults.json').read_text())[0]
            self.assertEqual(row['model_before'],row['model_after'])
            self.assertEqual(row['ready_after'],200)
            self.assertTrue((stage.out/'kill-trace-worker.ack').is_file())


if __name__=='__main__':unittest.main()

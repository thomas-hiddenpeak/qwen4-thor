"""Owned isolated usage I/O with real stores, child timeouts and safe recovery."""
import os
from pathlib import Path
import signal
import tempfile
import time
import unittest
import uuid

from tools.serve_recovery import access_io, access_policy, access_store, ledger
from tools.serve_recovery import quota_policy, quota_store, usage_io, usage_store
from tools.serve_recovery.test_gateway import config
from tools.serve_recovery.test_quota_admin import ROOT
from tools.serve_recovery.test_usage_store import owner, snapshot


class UsageIoTests(unittest.TestCase):
    def setUp(self):
        base=ROOT/'.q4t-work/serve-access-20261109/usage-io-fixtures';base.mkdir(parents=True,exist_ok=True)
        temp=tempfile.TemporaryDirectory(dir=base);self.addCleanup(temp.cleanup)
        self.state=Path(temp.name);self.binding=access_store.binding(self.state,str(uuid.uuid4()))
        self.owner=owner();usage_store.initialize_bound(self.binding)

    def close(self, value):
        end=time.monotonic()+3
        while not value.close() and time.monotonic()<end:time.sleep(.01)
        self.assertIsNone(value.process);self.assertIsNone(value.claim)

    def worker(self):
        result=usage_io.Worker(self.binding);self.addCleanup(self.close,result);return result

    def until(self, worker, predicate, seconds=4):
        end=time.monotonic()+seconds
        while time.monotonic()<end:
            worker.step()
            if predicate():return
            time.sleep(.01)
        self.fail('usage worker deadline: '+str(worker.status()))

    def test_independent_worker_large_seal_replacement_and_durable_confirmation(self):
        access_store.initialize_bound(self.binding,access_policy.from_keys([dict(id='alpha',api_keys=['a'*40])]))
        quota_store.initialize_bound(self.binding,quota_policy.from_base(config(self.state)['backend_policy']))
        others=[]
        for kind in ('access','limits'):
            r=access_io.Reader(self.state,self.binding['service_id'],self.binding['state_identity'],kind=kind)
            self.addCleanup(self.close,r);self.until(r,lambda:r.available);others.append(r)
        worker=self.worker();worker.begin(1,self.owner)
        self.assertFalse(worker.applied());self.until(worker,worker.applied)
        self.assertEqual(worker.value['phase'],'active')
        value=snapshot(time.monotonic_ns(),64)
        worker.seal(1,self.owner,value)
        self.assertFalse(worker.applied());self.until(worker,worker.applied)
        self.assertEqual(worker.value['snapshot'],value)
        pid=worker.process.pid
        worker.begin(2,self.owner);self.until(worker,worker.applied)
        self.assertEqual(worker.value['source']['generation'],1)
        self.assertEqual(worker.process.pid,pid)
        self.assertEqual(len({pid,*(r.process.pid for r in others)}),3)
        self.assertFalse(worker.status()['model_actions'])

    def test_stopped_worker_is_reaped_before_replacement_and_budget_is_bounded(self):
        worker=self.worker();worker.begin(1,self.owner)
        for attempt in range(3):
            self.until(worker,worker.applied)
            child=worker.process
            os.kill(child.pid,signal.SIGSTOP);worker.next_read=0
            self.until(worker,lambda:worker.error=='reader_timeout')
            self.assertFalse(worker.applied());self.assertTrue(worker.status()['worker_unreaped'])
            self.until(worker,lambda:worker.process is None)
            self.assertEqual(child.returncode,-signal.SIGKILL)
            self.assertEqual(worker.reaped,attempt+1)
        self.until(worker,lambda:worker.error=='reader_budget')
        self.assertEqual(worker.started,3)
        with usage_store.open_bound(self.binding) as persisted:
            self.assertEqual(persisted.head['state']['policy']['generation'],1)
            self.assertEqual(persisted.head['state']['policy']['revision'],1)

    def test_unknown_storage_never_claims_application_or_reinitializes(self):
        worker=self.worker();worker.begin(1,self.owner);self.until(worker,worker.applied)
        pid=worker.process.pid
        with usage_store.open_bound(self.binding):
            worker.next_read=0;self.until(worker,lambda:worker.error=='writer_busy')
            self.assertFalse(worker.applied())
        path=self.state/'client-usage/policy.json';saved=path.read_bytes();path.write_bytes(b'{')
        worker.next_read=0;self.until(worker,lambda:worker.error=='state_invalid')
        self.assertFalse(worker.applied());self.assertIsNone(worker.value)
        path.write_bytes(saved);worker.next_read=0;self.until(worker,worker.applied)
        self.assertEqual(worker.process.pid,pid)
        path.unlink();worker.next_read=0;self.until(worker,lambda:worker.error=='state_unavailable')
        self.assertFalse(worker.applied());self.assertFalse(path.exists())
        self.assertEqual(worker.started,1)


if __name__ == '__main__':unittest.main()

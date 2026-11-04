"""Supervisor phase deadlines and stop precedence; no HTTP/model required."""
import time
import threading
import unittest
from unittest import mock
from tools.serve_service.test_service import Case
from tools.serve_service import package as pkg,supervisor as sup


class Deadlines(unittest.TestCase):
    def setUp(self):
        self.fixture=Case()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def events(self):
        return (self.fixture.root/'runtime/events.log').read_text().splitlines()

    def test_late_first_ready_is_rejected_before_publication(self):
        calls=[]
        def observe(pid,_port):
            calls.append(pid)
            if len(calls)==1:time.sleep(.15)
            else:pkg.save(self.fixture.root/'runtime/stop.request.json',{})
            return self.fixture.observation(pid)
        code,state=self.fixture.run_supervisor(observe,restarts=3)
        self.assertEqual(code,1)
        self.assertEqual(state['error'],'startup deadline exceeded')
        self.assertEqual((state['attempts'],state['restarts']),(1,0))
        self.assertFalse(any(row.endswith(' READY') for row in self.events()))
        self.assertTrue(state['cleanup_confirmed'])

    def test_late_recovery_cannot_clear_observation_deadline(self):
        calls=[]
        def observe(pid,_port):
            calls.append(pid)
            if len(calls)==2:raise sup.contracts.InvalidObservation('http_deadline')
            if len(calls)==3:time.sleep(.15)
            if len(calls)>3:pkg.save(self.fixture.root/'runtime/stop.request.json',{})
            return self.fixture.observation(pid)
        code,state=self.fixture.run_supervisor(observe,restarts=3)
        self.assertEqual(code,1)
        self.assertEqual(state['error'],'observation deadline exceeded')
        self.assertEqual(sum(row.endswith(' READY') for row in self.events()),1)
        self.assertEqual((state['attempts'],state['restarts']),(1,0))
        self.assertTrue(state['cleanup_confirmed'])

    def test_stop_during_probe_never_publishes_ready(self):
        def observe(pid,_port):
            pkg.save(self.fixture.root/'runtime/stop.request.json',{})
            return self.fixture.observation(pid)
        code,state=self.fixture.run_supervisor(observe,restarts=3)
        self.assertEqual(code,0)
        self.assertEqual(state['state'],'STOPPED')
        self.assertEqual((state['attempts'],state['restarts']),(1,0))
        self.assertFalse(any(row.endswith(' READY') for row in self.events()))
        self.assertTrue(state['cleanup_confirmed'])

    def test_stop_wins_over_late_failure_and_expiry(self):
        def observe(_pid,_port):
            time.sleep(.15)
            pkg.save(self.fixture.root/'runtime/stop.request.json',{})
            raise sup.contracts.InvalidObservation('http_deadline')
        code,state=self.fixture.run_supervisor(observe,restarts=3)
        self.assertEqual((code,state['state'],state['attempts']),(0,'STOPPED',1))
        self.assertFalse(any(row.endswith(' OBSERVATION_UNAVAILABLE') for row in self.events()))
        self.assertTrue(state['cleanup_confirmed'])

    def test_late_validation_is_also_checked_before_ready(self):
        def observe(pid,_port):
            return self.fixture.observation(pid)
        original=sup.classify
        def classify(*args):
            time.sleep(.15)
            return original(*args)
        code,state=self.fixture.run_supervisor(observe,
            patch=mock.patch.object(sup,'classify',side_effect=classify))
        self.assertEqual((code,state['error']),(1,'startup deadline exceeded'))
        self.assertFalse(any(row.endswith(' READY') for row in self.events()))

    def test_default_probe_receives_deadline_and_live_stop_predicate(self):
        def probe(pid,_port,_ops,*,deadline,stopped):
            self.assertGreater(deadline,time.monotonic())
            self.assertLessEqual(deadline,time.monotonic()+.1)
            self.assertFalse(stopped())
            pkg.save(self.fixture.root/'runtime/stop.request.json',{})
            self.assertTrue(stopped())
            return self.fixture.observation(pid)
        code,state=self.fixture.run_supervisor(None,
            patch=mock.patch.object(sup,'probe',side_effect=probe))
        self.assertEqual((code,state['state']),(0,'STOPPED'))

    def test_poll_wait_cannot_extend_startup_deadline(self):
        calls=[]
        def observe(_pid,_port):
            calls.append(time.monotonic())
            raise sup.contracts.InvalidObservation('http_deadline')
        started=time.monotonic()
        code,state=self.fixture.run_supervisor(observe,
            config_overrides=dict(poll_seconds=2))
        self.assertEqual((code,state['error']),(1,'startup deadline exceeded'))
        self.assertEqual(len(calls),1)
        self.assertLess(time.monotonic()-started,1)

    def test_stop_file_interrupts_long_poll_wait(self):
        observed=threading.Event()
        def observe(pid,_port):
            observed.set()
            return self.fixture.observation(pid)
        def stop():
            if observed.wait(2):
                time.sleep(.03)
                pkg.save(self.fixture.root/'runtime/stop.request.json',{})
        thread=threading.Thread(target=stop)
        thread.start()
        try:
            started=time.monotonic()
            code,state=self.fixture.run_supervisor(observe,
                config_overrides=dict(poll_seconds=2))
            self.assertEqual((code,state['state']),(0,'STOPPED'))
            self.assertLess(time.monotonic()-started,1)
        finally:thread.join(3)


if __name__=='__main__':unittest.main(verbosity=2)

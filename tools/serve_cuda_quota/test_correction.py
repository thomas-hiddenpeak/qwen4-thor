"""CPU validation of waitable child custody and guard observation ordering."""
import json
from pathlib import Path
import subprocess
import unittest

from tools.serve_cuda_quota import correction as c


def query(begin=10, end=11):
    return dict(status='OK', request_id='fixture', stdout='\n'.join(json.dumps(row)
        for row in (dict(event='begin', monotonic=begin), dict(event='result', monotonic=end))))


class Custody(unittest.TestCase):
    def test_wait_without_reaping(self):
        child = subprocess.Popen(['/usr/bin/python3','-I','-B','-c','raise SystemExit(7)'])
        try:
            identity = c.proc(child.pid)
            self.assertEqual(c.wait_without_reaping(child, 5), 7)
            after = c.proc(child.pid)
            self.assertEqual(after['state'], 'Z')
            self.assertEqual(after['start_ticks'], identity['start_ticks'])
            self.assertIsNone(child.returncode)
            self.assertEqual(child.wait(timeout=1), 7)
            self.assertFalse(Path(f'/proc/{child.pid}').exists())
        finally:
            if child.poll() is None: child.kill()
            child.wait(timeout=5)

    def test_timeout_only_owned_child(self):
        child = subprocess.Popen(['/usr/bin/python3','-I','-B','-c',
                                  'import time; time.sleep(30)'])
        try:
            with self.assertRaises(TimeoutError): c.wait_without_reaping(child, 0.02)
            self.assertEqual(child.returncode, -9)
            self.assertFalse(Path(f'/proc/{child.pid}').exists())
        finally:
            if child.poll() is None: child.kill()
            child.wait(timeout=5)

    def test_fresh_observation(self):
        monitor = dict(event='monitor', monotonic=12, conflicts=[], unreadable_processes=0)
        result = c.acknowledgement([monitor], [query()], 9)
        self.assertEqual(result['query_begin'], 10)
        self.assertEqual(result['monitor'], monitor)

    def test_old_or_incomplete_observation(self):
        monitor = dict(event='monitor', monotonic=12, conflicts=[], unreadable_processes=0)
        self.assertIsNone(c.acknowledgement([monitor], [query()], 10.5))
        self.assertIsNone(c.acknowledgement([monitor], [], 9))
        self.assertIsNone(c.acknowledgement([], [query()], 9))
        self.assertIsNone(c.acknowledgement([monitor], [query(12,13)], 9))

    def test_guard_failure_is_not_ack(self):
        for field,value in (('conflicts',[dict(pid=123)]), ('unreadable_processes',1)):
            monitor = dict(event='monitor',monotonic=12,conflicts=[],unreadable_processes=0)
            monitor[field] = value
            with self.assertRaises(RuntimeError):
                c.acknowledgement([monitor],[query()],9)
        with self.assertRaises(RuntimeError):
            c.acknowledgement([dict(event='monitor',monotonic=12)],
                              [dict(status='TIMEOUT')],9)

    def test_identity_mismatch_is_not_custody(self):
        before = [dict(pid=i+20,start_ticks=i+100) for i in range(4)]
        current = [dict(row,state='Z',ppid=10,pgid=10) for row in before]
        c.validate_zombies(before,current,10,10)
        for field,value in (('pid',123),('start_ticks',999),('state','S'),('ppid',1),('pgid',1)):
            changed = [dict(row) for row in current]
            changed[0][field] = value
            with self.assertRaises(RuntimeError):
                c.validate_zombies(before,changed,10,10)


if __name__ == '__main__': unittest.main()

"""Driver identity, bounded work and real waitable-child custody checks."""
import copy
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest import mock

from tools.serve_watchdog import core as c, stage as s


class DriverTests(unittest.TestCase):
    def setUp(self):
        parent = s.ROOT/'build/serve-watchdog-driver-tests'
        parent.mkdir(exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=parent))

    def test_real_child_exit_remains_waitable_until_explicit_reap(self):
        with mock.patch.object(s, 'OUT', self.root):
            run = s.Run(dict(watchdog=dict(policy_sha256='a'*64)))
            run.spawn('fixture', [sys.executable, '-c', 'raise SystemExit(7)'])
            child = run.children['fixture']
            try:
                self.assertEqual(s.waitable(child, 5), 7)
                self.assertFalse(s.live(child))
                owner = s.n.sup.process_identity(child.pid)
                self.assertEqual(owner, run.owners['fixture'])
                self.assertEqual(child.returncode, None)
                self.assertEqual(s.waitable(child, 1), 7)
                stat = Path(f'/proc/{child.pid}/stat').read_text().rsplit(')', 1)[1].split()
                self.assertEqual(stat[0], 'Z')
            finally:
                child.wait(timeout=5)
                run.logs['fixture'].thread.join(5)
            self.assertFalse(Path(f'/proc/{child.pid}').exists())

    def test_expired_budget_rejects_before_new_process(self):
        run = s.Run(dict(watchdog=dict(policy_sha256='a'*64)))
        run.started -= s.LIMITS['business_seconds']+1
        with mock.patch.object(s.subprocess, 'Popen') as child:
            with self.assertRaisesRegex(c.ContractError, 'business_deadline'):
                run.spawn('must-not-run', ['/bin/true'])
            child.assert_not_called()

    def test_guard_acknowledgement_requires_fresh_complete_query(self):
        lease = self.root/'lease'; lease.mkdir()
        event = dict(event='monitor', monotonic=5, conflicts=[], unreadable_processes=0)
        query = dict(status='OK', request_id='fixture', stdout='\n'.join(
            json.dumps(v) for v in [dict(event='begin', monotonic=3),
                                   dict(event='result', monotonic=4)]))
        (lease/'events.jsonl').write_text(json.dumps(event)+'\n{"partial":')
        (lease/'nvml-queries.jsonl').write_text(json.dumps(query)+'\n')
        with mock.patch.object(s, 'OUT', self.root):
            self.assertIsNone(s.acknowledgement(3.5))
            self.assertEqual(s.acknowledgement(3)['query_id'], 'fixture')
            event['conflicts'] = ['foreign']
            (lease/'events.jsonl').write_text(json.dumps(event)+'\n')
            with self.assertRaisesRegex(c.ContractError, 'guard_interference'):
                s.acknowledgement(3)

    def test_changed_identity_rejected_before_package_or_network(self):
        plan = dict(limits=s.LIMITS, ports=s.PORTS,
            sources={'tools/serve_watchdog/stage.py':'0'*64})
        with mock.patch.object(s.n.pkg, 'read', return_value=plan), \
             mock.patch.object(s.n, 'verify') as package, \
             mock.patch.object(s.socket, 'socket') as network:
            with self.assertRaisesRegex(c.ContractError, 'source_changed'):
                s.check()
            package.assert_not_called(); network.assert_not_called()


if __name__ == '__main__': unittest.main()

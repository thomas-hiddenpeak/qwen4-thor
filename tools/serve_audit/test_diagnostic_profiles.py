"""Reject mismatched request or process identities in a capacity handoff."""
from pathlib import Path
import tempfile
import unittest

from tools.serve_audit import diagnostic_profiles as diagnostics


class DiagnosticProfileTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log = Path(self.temp.name) / 'model.log'
        self.row = dict(pid=118, start_ticks=9470, stderr='socket:[312]',
                        producer_nonblocking=True, status=400,
                        body={'error': {'param': 'temperature', 'code': 'invalid_request'}},
                        trace='req-18', log_offset=6)
        self.identity = dict(pid=118, start_ticks=9470)
        self.log.write_bytes(b'ready\n[q4t][http] trace_id=req-18 status=400 error=invalid_request\n')

    def test_matching_request_process_and_log(self):
        diagnostics.check(self.row, self.identity, self.log)

    def test_other_request_or_arbitrary_log_offset(self):
        for change in ({'trace': 'req-19'}, {'log_offset': 0}, {'status': 200}):
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                diagnostics.check(dict(self.row, **change), self.identity, self.log)

    def test_reused_pid_or_blocking_destination(self):
        for change in ({'start_ticks': 9471}, {'producer_nonblocking': False},
                       {'stderr': 'pipe:[312]'}):
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                diagnostics.check(dict(self.row, **change), self.identity, self.log)

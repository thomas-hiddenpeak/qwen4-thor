"""Host-only memory observation contracts; no NVML or CUDA calls."""
import unittest
from unittest.mock import patch
import monitor_memory as monitor


class MemoryTests(unittest.TestCase):
    def observe(self, result):
        with patch.object(monitor.nvml_process_query, 'query', return_value=result):
            return monitor.gpu_observation({7})

    def test_failure_is_unknown(self):
        for status in ('ERROR', 'TIMEOUT', 'PROTOCOL_ERROR', 'CLEANUP_FAILED'):
            value, state = self.observe(dict(status=status, pids=None))
            self.assertIsNone(value)
            self.assertEqual(state, 'nvml_' + status.lower())

    def test_missing_memory_or_process_is_unknown(self):
        for records in (None, [], [dict(device=0, pid=8, used_bytes=123)]):
            self.assertIsNone(self.observe(dict(status='OK', process_memory=records))[0])

    def test_partial_unknown_does_not_become_known_sum(self):
        rows = [dict(device=0, pid=7, used_bytes=123),
                dict(device=1, pid=7, used_bytes=None)]
        self.assertEqual(self.observe(dict(status='OK', process_memory=rows)),
                         (None, 'unknown_matching_process'))

    def test_known_bytes_are_not_mib_and_foreign_process_excluded(self):
        rows = [dict(device=0, pid=7, used_bytes=123),
                dict(device=1, pid=7, used_bytes=456),
                dict(device=1, pid=8, used_bytes=None)]
        self.assertEqual(self.observe(dict(status='OK', process_memory=rows)), (579, 'ok'))


if __name__ == '__main__':
    unittest.main()

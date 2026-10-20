"""Host-only protocol, API failure, and timeout tests (no NVML calls)."""
import contextlib
import io
import json
import os
import subprocess
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

import nvml_process_query as monitor


class QueryTests(unittest.TestCase):
    def fake_output(self, rows, code=0):
        text = '\n'.join(json.dumps(row) for row in rows) + '\n'
        return monitor.supervise(
            [sys.executable, '-c', f'print({text!r}, end=""); exit({code})'],
            'fresh', 2)

    def final(self, **changes):
        row = dict(request_id='fresh', event='result', status='OK',
                   pids=[], device_count=1)
        row.update(changes)
        return row

    def test_empty_and_nonempty_success(self):
        for pids in [[], [7, 9, 7]]:
            result = self.fake_output([self.final(pids=pids)])
            self.assertEqual(result['status'], 'OK')
            self.assertEqual(result['pids'], sorted(set(pids)))

    def test_invalid_results_never_publish_pids(self):
        for rows, code in [([], 0), ([self.final(request_id='stale')], 0),
                           ([self.final(), self.final()], 0),
                           ([self.final(), {'request_id': 'fresh'}], 0),
                           ([self.final(pids=[True])], 0),
                           ([self.final(pids=[0])], 0),
                           ([self.final(device_count=0)], 0),
                           ([self.final()], 3)]:
            with self.subTest(rows=rows, code=code):
                result = self.fake_output(rows, code)
                self.assertEqual(result['status'], 'PROTOCOL_ERROR')
                self.assertIsNone(result['pids'])

    def test_explicit_failure_ignores_partial_pids(self):
        result = self.fake_output([self.final(status='ERROR', pids=[9],
                                             error='not supported')], 1)
        self.assertEqual(result['status'], 'ERROR')
        self.assertIsNone(result['pids'])

    def test_memory_records_preserve_unknown_and_validate_identity(self):
        records = [dict(device=0, pid=7, used_bytes=None)]
        result = self.fake_output([self.final(pids=[7], process_memory=records)])
        self.assertEqual(result['status'], 'OK')
        self.assertEqual(result['process_memory'], records)
        for bad in [[], records * 2, [dict(device=0, pid=8, used_bytes=1)],
                    [dict(device=0, pid=7, used_bytes=2**64-1)]]:
            result = self.fake_output([self.final(pids=[7], process_memory=bad)])
            self.assertEqual(result['status'], 'PROTOCOL_ERROR')
            self.assertIsNone(result['pids'])

    def test_timeout_reaps_worker_and_keeps_api_boundary(self):
        code = ('import time; print(\'{"request_id":"fresh",'
                '"event":"begin","api":"compute"}\', flush=True); '
                'time.sleep(30)')
        result = monitor.supervise([sys.executable, '-c', code], 'fresh', 0.3)
        self.assertEqual(result['status'], 'TIMEOUT')
        self.assertIn('compute', result['stdout'])
        self.assertIsNone(result['pids'])
        with self.assertRaises(ProcessLookupError):
            os.kill(result['worker_pid'], 0)

    def test_unreaped_worker_is_not_success(self):
        process = MagicMock(pid=123, returncode=None)
        process.communicate.side_effect = subprocess.TimeoutExpired('fake', 1)
        with patch.object(monitor.subprocess, 'Popen', return_value=process):
            result = monitor.supervise(['fake'], 'fresh', 1)
        self.assertEqual(result['status'], 'CLEANUP_FAILED')
        self.assertIsNone(result['pids'])
        process.kill.assert_called_once()

    def test_partial_device_query_failure_discards_pids(self):
        nvml = types.SimpleNamespace(
            nvmlInit=lambda: None, nvmlShutdown=MagicMock(),
            nvmlDeviceGetCount=lambda: 2,
            nvmlDeviceGetHandleByIndex=lambda index: index,
            nvmlDeviceGetComputeRunningProcesses_v3=MagicMock(
                side_effect=[[types.SimpleNamespace(pid=99)],
                             RuntimeError('device 1 unsupported')]))
        output = io.StringIO()
        with patch.dict(sys.modules, pynvml=nvml), contextlib.redirect_stdout(output):
            self.assertEqual(monitor.worker('fresh'), 1)
        final = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(final['status'], 'ERROR')
        self.assertIsNone(final['pids'])
        nvml.nvmlShutdown.assert_called_once()

    def test_shutdown_failure_discards_pids(self):
        nvml = types.SimpleNamespace(
            nvmlInit=lambda: None,
            nvmlShutdown=MagicMock(side_effect=RuntimeError('shutdown')),
            nvmlDeviceGetCount=lambda: 1,
            nvmlDeviceGetHandleByIndex=lambda index: index,
            nvmlDeviceGetComputeRunningProcesses_v3=lambda _: [])
        output = io.StringIO()
        with patch.dict(sys.modules, pynvml=nvml), contextlib.redirect_stdout(output):
            self.assertEqual(monitor.worker('fresh'), 1)
        self.assertIsNone(json.loads(output.getvalue().splitlines()[-1])['pids'])


if __name__ == '__main__':
    unittest.main()

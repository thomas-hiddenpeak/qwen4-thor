"""CPU parsing/lease contracts; no external collector or GPU execution."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    'mechanism_stage_test', Path(__file__).with_name('mechanism_stage.py'))
stage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stage)


class StageContracts(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)

    def test_only_complete_tagged_receipts_count(self):
        log = self.base/'log'
        log.write_text('other text\n[mechanism-ready] {"pid":7}\n'
                       '[mechanism-ready] {"pid":')
        self.assertEqual(stage.rows(log, 'mechanism-ready'), [{'pid': 7}])
        self.assertEqual(stage.rows(log, 'mechanism-error'), [])
        with log.open('a') as f:
            f.write('8}\n')
        self.assertEqual(len(stage.rows(log, 'mechanism-ready')), 2)

    def test_unlocked_or_wrong_parent_lease_rejected(self):
        lock = self.base/'lock'
        lock.write_text(json.dumps({'pid': -1, 'state': 'held'}))
        with self.assertRaises(RuntimeError):
            stage.verify_lease({'lock_path': str(lock)})
        with patch.object(stage.fcntl, 'flock', side_effect=BlockingIOError):
            with self.assertRaises(RuntimeError):
                stage.verify_lease({'lock_path': str(lock)})
            with patch.object(stage.os, 'getppid', return_value=-1):
                stage.verify_lease({'lock_path': str(lock)})

    def test_changed_frozen_source_stops_before_tool_work(self):
        path = self.base/'source'
        path.write_text('old')
        plan = {'files': {str(path): stage.sha(path)}}
        path.write_text('new')
        with self.assertRaisesRegex(RuntimeError, 'frozen identity'):
            stage.verify_files(plan)

    def test_exclusive_receipts_preserve_first_failure(self):
        path = self.base/'state.json'
        stage.save(path, {'status': 'STOPPED'})
        with self.assertRaises(FileExistsError):
            stage.save(path, {'status': 'PASS'})
        self.assertEqual(json.loads(path.read_text())['status'], 'STOPPED')

    def test_pidfd_cleanup_failure_preserves_env_abort_receipt(self):
        class Child:
            pid = 7
            returncode = 0
            def poll(self):
                return 0
        output = self.base/'cupti'
        identity = dict(pid=7, start_ticks=123)
        def receipts(_path, tag):
            return [{'pid': 7}] if tag == 'mechanism-config' else []
        with patch.object(stage.subprocess, 'Popen', return_value=Child()), \
                patch.object(stage.sync.manual, 'proc_identity', return_value=identity), \
                patch.object(stage.runner, 'pin_fixture', return_value=(identity, 99)), \
                patch.object(stage.runner, 'pidfd_exited', side_effect=OSError('observed failure')), \
                patch.object(stage, 'rows', side_effect=receipts), \
                patch.object(stage.signal, 'pidfd_send_signal') as sent, \
                patch.object(stage.os, 'close') as closed:
            with self.assertRaises(stage.runner.legacy.EnvironmentAbort):
                stage.capture({'binary': '/mock/probe'}, 'cupti', output,
                              stage.time.monotonic()+120)
        result = json.loads((output/'cleanup.json').read_text())
        self.assertEqual(result['status'], 'ENV_ABORT')
        self.assertTrue(result['forced'])
        self.assertTrue(result['errors'])
        self.assertEqual({call.args[0] for call in sent.call_args_list}, {99})
        closed.assert_called_once_with(99)

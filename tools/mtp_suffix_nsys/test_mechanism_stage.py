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

    def reuse_plan(self):
        raw, analysis, proof = [self.base/name for name in ('raw', 'analysis', 'proof')]
        raw.write_text('immutable captured records')
        result = dict(status='VALID_AND_REPRODUCED', eligible_nsys=True,
            local_mechanism_valid=True, errors=[], performance_acceptance=False,
            model_qualification=False, source=dict(path=str(raw), sha256=stage.sha(raw)))
        analysis.write_text(json.dumps(result))
        proof.write_text(json.dumps(dict(analysis_sha256=stage.sha(analysis),
            raw_sha256=stage.sha(raw), analyzer_sha256=stage.sha(
                Path(stage.__file__).with_name('mechanism_analyze.py')),
            binary_sha256='binary', runs=1, elapsed_seconds=0.1)))
        paths = dict(raw=str(raw), analysis=str(analysis), proof=str(proof))
        return dict(binary_sha256='binary', files={p: stage.sha(p) for p in paths.values()},
            reused_cupti={**paths, **{k+'_sha256': stage.sha(p) for k, p in paths.items()}})

    def test_reuse_requires_frozen_raw_and_analysis(self):
        plan = self.reuse_plan()
        self.assertTrue(stage.reused_cupti_result(plan)['eligible_nsys'])
        Path(plan['reused_cupti']['raw']).write_text('changed')
        with self.assertRaisesRegex(ValueError, 'identity differs'):
            stage.reused_cupti_result(plan)

    def test_reuse_rejects_stale_analyzer_or_noneligible_result(self):
        for change in ('analyzer', 'eligibility'):
            with self.subTest(change=change):
                plan = self.reuse_plan()
                field = 'proof' if change == 'analyzer' else 'analysis'
                path = Path(plan['reused_cupti'][field])
                data = json.loads(path.read_text())
                data['analyzer_sha256' if change == 'analyzer' else 'eligible_nsys'] = 'stale'
                path.write_text(json.dumps(data))
                plan['files'][str(path)] = plan['reused_cupti'][field+'_sha256'] = stage.sha(path)
                if field == 'analysis':
                    proof = Path(plan['reused_cupti']['proof'])
                    data = json.loads(proof.read_text())
                    data['analysis_sha256'] = stage.sha(path)
                    proof.write_text(json.dumps(data))
                    plan['files'][str(proof)] = plan['reused_cupti']['proof_sha256'] = stage.sha(proof)
                with self.assertRaises(ValueError):
                    stage.reused_cupti_result(plan)

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

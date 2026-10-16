"""Launcher ownership and cold-state evidence contracts (no model or sudo)."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from isolated_service import IsolatedService
from run_budget_experiment import payload_residual


class IsolationContracts(unittest.TestCase):
    def test_reject_existing_unit_without_stopping_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch('isolated_service.unit_properties', return_value={
                    'LoadState': 'loaded'}), patch('isolated_service.subprocess.run') as run:
                with self.assertRaisesRegex(RuntimeError, 'existing unit'):
                    IsolatedService(['/bin/true'], cwd=tmp, env={},
                                    log_path=Path(tmp) / 'log',
                                    unit='q4t-existing.service', memory_max=1024,
                                    evidence=Path(tmp) / 'evidence')
                run.assert_not_called()

    def test_reject_non_experiment_unit(self):
        with patch('isolated_service.unit_properties') as props:
            with self.assertRaises(ValueError):
                IsolatedService([], cwd='.', env={}, log_path='log',
                                unit='ssh.service', memory_max=1024,
                                evidence='unused')
            props.assert_not_called()

    def test_reject_invalid_limit_before_any_service_action(self):
        with patch('isolated_service.unit_properties') as props:
            with self.assertRaises(ValueError):
                IsolatedService([], cwd='.', env={}, log_path='log',
                                unit='q4t-test.service', memory_max=-1,
                                evidence='unused')
            props.assert_not_called()

    def test_cleanup_cannot_pass_with_unit_remaining(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = IsolatedService.__new__(IsolatedService)
            service.unit = 'q4t-owned.service'
            service.evidence = Path(tmp)
            service.owned, service.closed = True, False
            service.snapshot = lambda _: None
            with patch('isolated_service.unit_properties', return_value={
                    'LoadState': 'loaded'}), patch('isolated_service.subprocess.run',
                    return_value=SimpleNamespace(returncode=1, stderr='failure')):
                with self.assertRaisesRegex(RuntimeError, 'cleanup incomplete'):
                    service.close()
            self.assertFalse(service.closed)
            self.assertTrue((Path(tmp) / 'cleanup.json').exists())

    def test_cold_gate_retains_unknown(self):
        self.assertIsNone(payload_residual({
            'complete_file_set_observed': False, 'files': []}))

    def test_payload_cache_gate_does_not_hide_warm_weights(self):
        observation = {'complete_file_set_observed': True, 'files': [
            {'path': '/model/model.safetensors', 'resident_bytes': 4096},
            {'path': '/model/ple/table.bin', 'resident_bytes': 8192},
            {'path': '/model/tokenizer.json', 'resident_bytes': 65536}]}
        self.assertEqual(payload_residual(observation), 12288)


if __name__ == '__main__':
    unittest.main()

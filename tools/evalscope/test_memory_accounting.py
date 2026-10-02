"""Host-only contracts for memory observation and fail-closed accounting."""
import csv
import json
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest

from memory_accounting import evaluate_memory_csv
from monitor_memory import parse_gpu_csv, summarize


class MemoryAccountingTest(unittest.TestCase):
    def fixture(self, directory, rows, summary):
        directory = Path(directory)
        with (directory / 'memory.csv').open('w', newline='') as file:
            writer = csv.DictWriter(file, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        (directory / 'memory-peak.json').write_text(json.dumps(summary))
        return directory

    def test_gpu_mib_is_converted_exactly_once(self):
        self.assertEqual(parse_gpu_csv('12, 3\n99, 100\n', {12}),
                         (3 * 1024 * 1024, 'ok'))
        rows = [{'t': 1, 'root': 12, 'phase': 'requests',
                 'rss_kb': 4, 'gpu_bytes': 3 * 1024 * 1024}]
        result = summarize(rows, {}, 12, 1, 1, 'target_exited', set())
        self.assertEqual(result['peaks_bytes']['gpu_bytes'], 3 * 1024 * 1024)
        self.assertEqual(result['peaks_bytes']['rss_kb'], 4 * 1024)
        self.assertNotIn('gpu_bytes', result['peaks_kb'])
        self.assertIsNone(result['service_total_physical_peak_bytes'])

    def test_matching_na_is_unknown_not_zero(self):
        self.assertEqual(parse_gpu_csv('12, [N/A]\n', {12}),
                         (None, 'unknown_matching_process'))
        self.assertEqual(parse_gpu_csv('99, 5\n', {12}),
                         (0, 'no_matching_compute_process'))

    def test_authorization_cannot_approve_missing_attribution(self):
        with tempfile.TemporaryDirectory() as directory:
            self.fixture(directory, [
                {'t': 1, 'root': 12, 'rss_kb': 4, 'gpu_bytes': 5,
                 'cached_kb': 100, 'private_kb': 3}],
                {'schema_version': 2, 'sampling_complete': True,
                 'prelaunch_sample_present': True})
            result = evaluate_memory_csv(directory, budget_bytes=1,
                                         allow_overrun=True)
            self.assertEqual(result['gate'], 'INDETERMINATE')
            self.assertFalse(result['approval_applied'])
            self.assertIsNone(result['candidate_total'])
            self.assertTrue(result['bounds']['budget_exceeded_by_known_private_residency'])

    def test_peaks_are_contemporaneous_only_and_nonphysical(self):
        with tempfile.TemporaryDirectory() as directory:
            self.fixture(directory, [
                {'t': 1, 'root': 12, 'rss_kb': 100, 'gpu_bytes': 1,
                 'cached_kb': 2},
                {'t': 2, 'root': 12, 'rss_kb': 1, 'gpu_bytes': 100 * 1024,
                 'cached_kb': 1}], {})
            result = evaluate_memory_csv(directory)
            diag = result['nonphysical_diagnostics']
            self.assertEqual(diag['max_sampled_rss_plus_driver_bytes'], 101 * 1024)
            self.assertFalse(diag['usable_as_physical_total_or_bound'])
            self.assertIsNone(result['candidate_total'])
            self.assertEqual(result['bounds']['instantaneous_model_file_cache_lower_bytes'], 0)
            self.assertIsNone(result['bounds']['instantaneous_model_file_cache_upper_bytes'])

    def test_unknown_gpu_is_preserved_and_legacy_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            self.fixture(directory, [
                {'t': 1, 'root': 12, 'rss_kb': 4, 'gpu_bytes': -1,
                 'cached_kb': 100}],
                {'service_total_physical_peak_bytes': 999,
                 'peaks_bytes': {'gpu_bytes': 1024 * 1024 * 1024}})
            result = evaluate_memory_csv(directory)
            self.assertEqual(result['coverage']['gpu_unknown_samples'], 1)
            self.assertIsNone(result['observed_component_peaks_bytes']['nvidia_process_driver_accounted'])
            self.assertIn('legacy_gpu_freshness_unknown', result['unresolved'])
            self.assertFalse(result['historical_claims']['usable_as_complete_physical_total'])

    def test_missing_evidence_is_not_zero_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            result = evaluate_memory_csv(directory)
            self.assertEqual(result['gate'], 'INDETERMINATE')
            self.assertIn('missing_memory_csv', result['unresolved'])
            self.assertIsNone(result['bounds']['instantaneous_model_file_cache_upper_bytes'])

    def test_cross_time_cache_pss_sum_cannot_prove_instantaneous_overrun(self):
        with tempfile.TemporaryDirectory() as directory:
            self.fixture(directory, [
                {'t': 1, 'root': 12, 'rss_kb': 100, 'gpu_bytes': 1000000,
                 'cached_kb': 300, 'private_kb': 25, 'pss_anon_kb': 20,
                 'pss_shmem_kb': 5, 'model_file_cache_bytes': 50000,
                 'model_file_cache_known_bytes': 50000}],
                {'schema_version': 2, 'sampling_complete': True,
                 'prelaunch_sample_present': True})
            result = evaluate_memory_csv(directory, budget_bytes=60000)
            self.assertEqual(result['gate'], 'INDETERMINATE')
            self.assertIsNone(result['bounds']['observed_service_physical_lower_bytes'])
            self.assertEqual(result['sampling_window_estimates']['model_cache_plus_anon_shmem_pss_peak_bytes'],
                             50000 + 25 * 1024)
            self.assertFalse(result['sampling_window_estimates']['used_for_budget_gate'])
            self.assertNotIn('model_file_cache_attribution', result['unresolved'])
            self.assertIsNone(result['candidate_total'])
            approved = evaluate_memory_csv(directory, budget_bytes=60000,
                                            allow_overrun=True)
            self.assertEqual(approved['gate'], 'INDETERMINATE')

    def test_direct_cli_refuses_existing_evidence(self):
        scripts = Path(__file__).parent
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            artifact = directory / 'memory.csv'
            artifact.write_text('immutable evidence')
            run = subprocess.run([sys.executable, '-B', str(scripts / 'monitor_memory.py'),
                                  '--out', str(directory)], capture_output=True)
            self.assertNotEqual(run.returncode, 0)
            self.assertEqual(artifact.read_text(), 'immutable evidence')
            output = directory / 'gate.json'
            output.write_text('immutable gate')
            for script, args in [
                ('memory_accounting.py', ['--memory-dir', str(directory)]),
                ('file_cache.py', ['--file', str(artifact)])]:
                run = subprocess.run([sys.executable, '-B', str(scripts / script),
                                      *args, '--output', str(output)], capture_output=True)
                self.assertNotEqual(run.returncode, 0)
                self.assertEqual(output.read_text(), 'immutable gate')

    def test_summary_reports_incomplete_lifecycle_and_missing_gpu(self):
        rows = [{'t': 1, 'root': None, 'phase': 'before_start',
                 'rss_kb': None, 'gpu_bytes': None},
                {'t': 2, 'root': 12, 'phase': 'loading',
                 'rss_kb': 3, 'gpu_bytes': None}]
        result = summarize(rows, {}, 12, 1, 1, 'controller_stopped', {99})
        self.assertTrue(result['prelaunch_sample_present'])
        self.assertFalse(result['sampling_complete'])
        self.assertEqual(result['gpu_unknown_samples'], 1)
        self.assertEqual(result['existing_named_pids_at_start'], [99])


if __name__ == '__main__':
    unittest.main()

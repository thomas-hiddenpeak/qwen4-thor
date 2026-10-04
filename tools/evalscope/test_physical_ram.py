"""Host-only contracts for fail-closed, read-only physical RAM observations."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from physical_ram import (
    Binding, Summary, collect_sample, parse_dma_buf, parse_meminfo,
    parse_nvmap_clients, parse_nvmap_stat, read_observation, same_identity,
)


WORK = Path(__file__).resolve().parents[2] / '.q4t-work/physical-ram-tests'


class PhysicalRamTest(unittest.TestCase):
    def setUp(self):
        WORK.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=WORK)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write_process(self, pid=123, start=7):
        directory = self.root / 'proc' / str(pid)
        directory.mkdir(parents=True, exist_ok=True)
        fields = ['S', '1'] + ['0'] * 18
        fields[19] = str(start)
        (directory / 'stat').write_text(
            f'{pid} (process with spaces) ' + ' '.join(fields))
        (self.root / 'pid').write_text(str(pid))

    def observer(self, path, parser, privileged=False):
        self.reads.append((path, privileged))
        raw = {
            '/proc/meminfo': 'MemTotal: 100 kB\nMemFree: 25 kB\n',
            '/sys/kernel/debug/nvmap/iovmm/clients':
                'CLIENT PROCESS PID SIZE\ntotal 0K\n',
            '/sys/kernel/debug/nvmap/stats/total_memory': '0\n',
            '/sys/kernel/debug/dma_buf/bufinfo':
                'Dma-buf Objects:\nTotal 0 objects, 0 bytes\n',
        }[path]
        return {'status': 'OBSERVED', 'value': parser(raw),
                'duration_seconds': 0.001}

    def sample(self, due=True):
        self.reads = []
        return collect_sample(0, Binding(None), due, 'sudo', None,
                              observer=self.observer)

    def test_linux_units_and_global_scope(self):
        result = parse_meminfo(
            'MemTotal: 100 kB\nMemFree: 25 kB\n'
            'KReclaimable: 50 kB\nSReclaimable: 10 kB\n')
        self.assertEqual(result['global_linux_nonfree_bytes'], 75 * 1024)
        self.assertEqual(result['global_non_slab_reclaimable_bytes'], 40 * 1024)
        self.assertNotIn('service_physical_bytes', result)
        self.assertEqual(result['field_errors']['MemAvailable'], 'not_reported')

    def test_bad_units_duplicates_and_inconsistency_remain_unknown(self):
        result = parse_meminfo(
            'MemTotal: 10 kB\nMemFree: 20 kB\n'
            'Cached: 2 MB\nShmem: 3 kB\nShmem: 3 kB\n')
        self.assertIsNone(result['global_linux_nonfree_bytes'])
        self.assertNotIn('Cached', result['fields_bytes'])
        self.assertNotIn('Shmem', result['fields_bytes'])
        self.assertEqual(result['field_errors']['Shmem'], 'duplicate_field')
        with self.assertRaises(ValueError):
            parse_meminfo('MemTotal: unavailable\n')

    def test_nvmap_zero_cannot_prove_zero_cuda(self):
        value = parse_nvmap_clients('CLIENT PROCESS PID SIZE\ntotal 0K\n')
        self.assertEqual(value['reported_total_K'], 0)
        self.assertIsNone(value['cuda_physical_bytes'])
        self.assertIsNone(parse_nvmap_stat('0')['cuda_physical_bytes'])
        with self.assertRaises(ValueError):
            parse_nvmap_clients('total 10K\n')
        with self.assertRaises(ValueError):
            parse_nvmap_clients('CLIENT PROCESS PID SIZE\ntotal 1K\ntotal 2K')

    def test_dma_object_size_is_not_physical_union(self):
        result = parse_dma_buf('Dma-buf Objects:\nTotal 2 objects, 4096 bytes')
        self.assertEqual(result['reported_object_size_bytes'], 4096)
        self.assertIsNone(result['cuda_physical_bytes'])
        with self.assertRaises(ValueError):
            parse_dma_buf('Dma-buf Objects:\nTotal 0 objects, 4096 bytes')

    def test_read_failure_is_unknown_with_window(self):
        result = read_observation(self.root / 'missing', parse_meminfo)
        self.assertEqual(result['status'], 'UNKNOWN')
        self.assertIsNone(result['value'])
        self.assertEqual(result['error']['kind'], 'FileNotFoundError')
        self.assertLessEqual(result['start_monotonic_ns'],
                             result['end_monotonic_ns'])

    def test_source_truncation_does_not_parse_partial_total(self):
        source = self.root / 'long'
        source.write_text('MemTotal: 100 kB\n' + 'x' * 65536)
        result = read_observation(source, parse_meminfo)
        self.assertTrue(result['truncated'])
        self.assertEqual(result['status'], 'UNKNOWN')
        self.assertIsNone(result['value'])

    def test_sudo_failure_and_timeout_cannot_become_zero(self):
        failure = subprocess.CompletedProcess([], 1, b'', b'permission denied')
        with patch('physical_ram.subprocess.run', return_value=failure):
            result = read_observation('/fixed/path', parse_nvmap_stat, True)
        self.assertEqual(result['status'], 'UNKNOWN')
        self.assertIsNone(result['value'])
        with patch('physical_ram.subprocess.run', side_effect=
                   subprocess.TimeoutExpired(['sudo'], 2)):
            result = read_observation('/fixed/path', parse_nvmap_stat, True)
        self.assertEqual(result['error']['kind'], 'TimeoutExpired')

    def test_decimation_never_carries_forward_driver_zero(self):
        first = self.sample()
        self.assertEqual(len(self.reads), 4)
        skipped = self.sample(False)
        self.assertEqual(len(self.reads), 1)
        for name in first['sources']:
            if name != 'meminfo':
                self.assertEqual(skipped['sources'][name]['status'],
                                 'SCHEDULED_SKIP')
                self.assertIsNone(skipped['sources'][name]['value'])

    def test_pid_reuse_is_not_a_fresh_binding(self):
        self.write_process()
        binding = Binding(self.root / 'pid', self.root / 'proc')
        first = binding.observe()
        self.assertEqual(first['status'], 'LIVE')
        self.write_process(start=8)
        changed = binding.observe()
        self.assertEqual(changed['status'], 'IDENTITY_CHANGED')
        self.assertFalse(same_identity(first, changed))
        (self.root / 'pid').unlink()
        self.assertEqual(binding.observe()['status'], 'UNKNOWN')

    def test_summary_never_signs_physical_acceptance_or_sums_peaks(self):
        summary = Summary()
        summary.add(self.sample())
        summary.add(self.sample(False))
        result = summary.result('STOP_FILE', {})
        self.assertEqual(result['gate'], 'INDETERMINATE')
        for key in ('complete_service_physical_peak_bytes',
                    'strict_service_physical_lower_bound_bytes',
                    'strict_service_physical_upper_bound_bytes'):
            self.assertIsNone(result[key])
        self.assertEqual(result['source_status_counts']['dma_buf_bufinfo'],
                         {'OBSERVED': 1, 'SCHEDULED_SKIP': 1})
        self.assertFalse(result['claims_complete_lifecycle'])
        self.assertFalse(result['claims_cuda_coverage_from_empty_debugfs'])

    def test_cli_preserves_existing_evidence(self):
        existing = self.root / 'samples.jsonl'
        existing.write_text('immutable')
        script = Path(__file__).with_name('physical_ram.py')
        result = subprocess.run([sys.executable, '-B', str(script),
                                 '--output-dir', str(self.root)],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('refusing to overwrite', result.stderr)
        self.assertEqual(existing.read_text(), 'immutable')
        self.assertFalse((self.root / 'identity.json').exists())

    def test_cli_stop_file_writes_one_endpoint_without_live_queries(self):
        import physical_ram
        output = self.root / 'out'
        stop = self.root / 'stop'
        self.reads = []
        original_collect = physical_ram.collect_sample

        def collect(*args, **kwargs):
            kwargs['observer'] = self.observer
            result = original_collect(*args, **kwargs)
            stop.write_text('stop')
            return result

        args = ['physical_ram.py', '--output-dir', str(output),
                '--stop-file', str(stop), '--driver-mode', 'direct']
        with patch.object(sys, 'argv', args), \
                patch('physical_ram.collect_sample', side_effect=collect), \
                patch('physical_ram.signal.signal'):
            self.assertEqual(physical_ram.main(), 0)
        rows = [json.loads(row) for row in
                (output / 'samples.jsonl').read_text().splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertFalse(rows[0]['endpoint'])
        self.assertTrue(rows[1]['endpoint'])
        summary = json.loads((output / 'summary.json').read_text())
        self.assertEqual(summary['stop_reason'], 'STOP_FILE')
        self.assertEqual(len(summary['samples_sha256']), 64)


if __name__ == '__main__':
    unittest.main()

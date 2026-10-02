"""Host contracts for decimation. GPU/model work is always stubbed."""
import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import monitor_memory as monitor


WORK = Path(__file__).resolve().parents[2] / '.q4t-work/monitor-schedule-tests'


class MonitorScheduleTests(unittest.TestCase):
    def test_endpoint_mode_never_observes_live_cache(self):
        schedule = monitor.ObservationSchedule(10, 'endpoints')
        self.assertTrue(schedule.cache_due(True, 'before_start'))
        self.assertFalse(schedule.cache_due(False, 'before_start'))
        for phase in ('startup', 'requests:context-45056:run1', 'shutdown'):
            self.assertFalse(schedule.cache_due(True, phase))
            self.assertFalse(schedule.cache_due(False, phase))
        self.assertTrue(schedule.cache_due(False, 'after_exit'))

    def test_legacy_every_sample_unchanged(self):
        schedule = monitor.ObservationSchedule(1, 'every_sample')
        self.assertTrue(schedule.cache_due(False, 'requests'))
        self.assertTrue(schedule.cache_due(False, 'before_start'))

    def test_gpu_due_uses_monotonic_and_does_not_invent_values(self):
        schedule = monitor.ObservationSchedule(10, 'endpoints')
        self.assertFalse(schedule.gpu_due(100, False))
        self.assertTrue(schedule.gpu_due(100, True))
        schedule.gpu_last_monotonic = 100
        schedule.gpu_last_end_t = 5000
        self.assertFalse(schedule.gpu_due(109.9, True))
        self.assertTrue(schedule.gpu_due(110, True))
        freshness = schedule.freshness(5009)
        self.assertEqual(freshness['gpu_observation_age_seconds'], 9)
        self.assertIsNone(freshness['model_file_cache_observation_age_seconds'])
        self.assertNotIn('gpu_bytes', freshness)

    def test_endpoint_cache_is_not_a_live_peak(self):
        rows = [dict(phase='before_start', root=None, gpu_bytes=None,
                     model_file_cache_bytes=999, model_file_cache_fresh=True),
                dict(phase='requests', root=12, gpu_bytes=10, gpu_fresh=True,
                     model_file_cache_bytes=None, model_file_cache_fresh=False),
                dict(phase='requests', root=12, gpu_bytes=None, gpu_fresh=False,
                     gpu_status='scheduled_skip', model_file_cache_bytes=None),
                dict(phase='requests', root=12, gpu_bytes=None, gpu_fresh=True,
                     gpu_status='query_failed', model_file_cache_bytes=None),
                dict(phase='after_exit', root=None, gpu_bytes=None,
                     model_file_cache_bytes=888, model_file_cache_fresh=True)]
        summary = monitor.summarize(rows, {}, 12, 1, 1, 'target_exited', [],
                                    10, 'endpoints')
        self.assertEqual(summary['gpu_peak_bytes'], 10)
        self.assertEqual(summary['gpu_scheduled_skip_samples'], 1)
        self.assertEqual(summary['gpu_failed_observations'], 1)
        self.assertEqual(summary['model_file_cache_observed_samples'], 2)
        self.assertEqual(summary['model_file_cache_live_observed_samples'], 0)
        self.assertIsNone(summary['model_file_cache_live_peak_bytes'])
        self.assertNotIn('model_file_cache_bytes', summary['peaks_bytes'])
        self.assertEqual(len(summary['model_file_cache_endpoint_observations']), 2)

    def test_stubbed_lifecycle_sidecars_keep_skips_and_endpoints(self):
        WORK.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=WORK) as temp:
            directory = Path(temp)
            payload = directory / 'small-host-file'
            payload.write_bytes(b'fixture')
            pidfile = directory / 'server.pid'
            output = directory / 'memory'
            calls = []

            def observe(paths):
                calls.append(paths)
                return dict(start_t=10, end_t=11, resident_bytes=100,
                            known_resident_lower_bytes=100, observed_files=1,
                            errors=[], files=[dict(path=str(payload), cached_pages=1)])

            def advance(_delay):
                pidfile.write_text('123')

            argv = ['monitor', '--pid-file', str(pidfile), '--out', str(output),
                    '--model-dir', str(directory), '--interval', '1',
                    '--gpu-interval', '10', '--file-cache-mode', 'endpoints']
            with patch('sys.argv', argv), patch.object(monitor, 'STOP', False), \
                    patch.object(monitor, 'find_pids', return_value=[]), \
                    patch.object(monitor, 'tree', return_value={123}), \
                    patch.object(monitor, 'process_stat', side_effect=[(1, 7), (1, 7), None]), \
                    patch.object(monitor, 'process_memory', return_value={k: 1 for k in monitor.PROCESS_KEYS}), \
                    patch.object(monitor, 'model_files', return_value=[payload]), \
                    patch.object(monitor, 'observe_files', side_effect=observe), \
                    patch.object(monitor, 'gpu_observation', return_value=(1234, 'ok')) as gpu, \
                    patch.object(monitor.signal, 'signal'), \
                    patch.object(monitor, 'ResourceSampler') as sampler, \
                    patch.object(monitor, 'resource_summary', side_effect=lambda rows: dict(sample_count=len(list(rows)))), \
                    patch.object(monitor.time, 'sleep', side_effect=advance):
                sampler.return_value.sample.return_value = {}
                self.assertEqual(monitor.main(), 0)
                self.assertEqual(gpu.call_count, 1)
            rows = list(csv.DictReader((output / 'memory.csv').open()))
            cache = [json.loads(line) for line in (output / 'model-cache.jsonl').read_text().splitlines()]
            resources = [json.loads(line) for line in (output / 'resource-samples.jsonl').read_text().splitlines()]
            self.assertEqual(len(calls), 2)
            self.assertEqual(len(rows), len(cache))
            self.assertEqual(len(rows), len(resources))
            self.assertEqual([row['phase'] for row in rows],
                             ['before_start', 'loading_or_requests', 'loading_or_requests', 'after_exit'])
            self.assertEqual([row['observed'] for row in cache], [True, False, False, True])
            self.assertEqual(rows[2]['gpu_status'], 'scheduled_skip')
            self.assertEqual(rows[2]['gpu_bytes'], '')
            self.assertEqual(rows[1]['model_file_cache_bytes'], '')
            self.assertEqual(cache[1]['policy'], 'endpoints')
            self.assertNotIn('resident_pages', cache[1])


if __name__ == '__main__':
    unittest.main()

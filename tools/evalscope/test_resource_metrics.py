"""Host-only resource contracts; no GPU, model reads or cgroup mutations."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from resource_metrics import (ResourceSampler, collect_resources, counter_delta,
                              disk_counters, model_devices, pressure, read_cgroup,
                              resource_summary)


WORK = Path(__file__).resolve().parents[2] / '.q4t-work/resource-metrics-tests'


class ResourceMetricsTest(unittest.TestCase):
    def setUp(self):
        WORK.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=WORK)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.proc = self.root / 'proc'
        self.cgroot = self.root / 'cgroup'
        self.cg = self.cgroot / 'system.slice/test.service'
        self.sysdev = self.root / 'sysdev'
        self.cg.mkdir(parents=True)
        self.sysdev.mkdir()
        self.roots = {'proc_root': self.proc, 'cgroup_root': self.cgroot,
                      'sys_dev_root': self.sysdev}
        self.write_process()
        values = {
            'memory.current': '1200', 'memory.peak': '2400',
            'memory.stat': 'anon 100\nfile 800\nshmem 600\nkernel 300',
            'memory.events': 'low 0\nhigh 0\nmax 0\noom 0\noom_kill 0',
            'memory.events.local': 'low 0\nhigh 0\nmax 0\noom 0\noom_kill 0',
            'memory.swap.current': '0', 'memory.swap.max': '0',
            'memory.swap.events': 'high 0\nmax 0\nfail 0',
            'memory.max': '1073741824', 'memory.high': 'max',
            'io.stat': '259:0 rbytes=4096 wbytes=8192 rios=1 wios=2',
            'cgroup.events': 'populated 1\nfrozen 0',
            'cgroup.procs': '123', 'pids.current': '1',
        }
        for name, text in values.items():
            (self.cg / name).write_text(text + '\n')

    def write_process(self, start=7, read_bytes=4096):
        directory = self.proc / '123'
        directory.mkdir(parents=True, exist_ok=True)
        fields = ['S', '1'] + ['0'] * 18
        fields[19] = str(start)
        (directory / 'stat').write_text('123 (worker with spaces) ' + ' '.join(fields))
        (directory / 'cgroup').write_text('0::/system.slice/test.service\n')
        (directory / 'io').write_text(
            f'rchar: 999999\nwchar: 8192\nsyscr: 40\nsyscw: 2\n'
            f'read_bytes: {read_bytes}\nwrite_bytes: 8192\ncancelled_write_bytes: 0\n')

    def test_real_pid_identity_and_cgroup_binding(self):
        sample = collect_resources(123, expected_cgroup=self.cg, **self.roots)
        self.assertEqual(sample['binding']['status'], 'bound_to_live_pid')
        self.assertTrue(sample['binding']['expected_matches_actual'])
        proc = sample['processes'][0]
        self.assertEqual(proc['identity'], {'pid': 123, 'start_ticks': 7})
        self.assertEqual(proc['io']['value']['rchar'], 999999)
        self.assertEqual(proc['io']['value']['read_bytes'], 4096)
        self.assertLessEqual(sample['start_t'], sample['end_t'])
        other = collect_resources(123, expected_cgroup='/other.service', **self.roots)
        self.assertEqual(other['binding']['status'], 'mismatch')
        self.assertFalse(other['binding']['expected_matches_actual'])

    def test_missing_psi_and_malformed_values_are_unknown(self):
        sample = read_cgroup(self.cg, self.cgroot)
        observations = sample['observations']
        self.assertEqual(observations['memory.max']['value'], 1073741824)
        self.assertEqual(observations['memory.high']['value'], 'max')
        self.assertIsNone(observations['memory.pressure']['value'])
        self.assertEqual(observations['memory.pressure']['error']['errno'], 2)
        (self.cg / 'memory.current').write_text('not-a-number')
        self.assertIsNone(read_cgroup(self.cg, self.cgroot)['observations']['memory.current']['value'])
        (self.cg / 'memory.stat').write_text('')
        self.assertIsNone(read_cgroup(self.cg, self.cgroot)['observations']['memory.stat']['value'])

    def test_reset_gap_and_new_identity_never_become_zero(self):
        self.assertEqual(counter_delta({'r': 9}, {'r': 5})['value'], {'r': 4})
        reset = counter_delta({'r': 4}, {'r': 9})
        self.assertIsNone(reset['value']['r'])
        self.assertEqual(reset['error']['r'], 'counter_decreased_or_reset')
        self.assertIsNone(counter_delta({'r': 4}, None)['value'])
        self.assertIsNone(counter_delta(None, {'r': 4})['value'])
        self.assertEqual(counter_delta({'r': 8}, {'r': 4}, False)['error']['reason'], 'identity_changed')
        self.assertEqual(counter_delta({'r': 2**60+8}, {'r': 2**60})['value']['r'], 8)
        self.assertIsNone(counter_delta({}, {'r': 4})['value']['r'])

    def test_pid_reuse_and_io_gaps_are_not_bridged(self):
        sampler = ResourceSampler(**self.roots)
        sampler.sample(123)
        self.write_process(start=8, read_bytes=8192)
        reused = sampler.sample(123)['processes'][0]['io_delta']
        self.assertEqual(reused['error']['reason'], 'identity_changed')
        (self.proc / '123/io').unlink()
        self.assertIsNone(sampler.sample(123)['processes'][0]['io']['value'])
        self.write_process(start=8, read_bytes=16384)
        resumed = sampler.sample(123)['processes'][0]['io_delta']
        self.assertIsNone(resumed['value'])
        self.assertEqual(resumed['error']['reason'], 'missing_previous')

    def test_retained_cgroup_is_read_after_target_exit(self):
        sampler = ResourceSampler(**self.roots)
        first = sampler.sample(123)
        first.update(sequence=0, phase='requests')
        (self.cg / 'cgroup.procs').write_text('999\n')  # stop helper, not q4t
        (self.cg / 'memory.peak').write_text('2500\n')
        final = sampler.sample(None, pids=[])
        final.update(sequence=1, phase='after_exit')
        self.assertEqual(final['binding']['status'], 'configured_path_without_live_pid')
        self.assertEqual(final['cgroup']['observations']['cgroup.procs']['value'], [999])
        self.assertEqual(final['processes'], [])
        summary = resource_summary(iter([first, final]))
        self.assertEqual(summary['last_live_pid_sample']['sequence'], 0)
        self.assertEqual(summary['final_sample']['sequence'], 1)
        self.assertEqual(summary['observed_cgroup_counter_peaks_bytes']['memory.peak'], 2500)

    def test_cgroup_replacement_and_counter_reset(self):
        sampler = ResourceSampler(**self.roots)
        sampler.sample(123)
        (self.cg / 'io.stat').write_text('259:0 rbytes=100 wbytes=8192 rios=1 wios=2\n')
        reset = sampler.sample(123)['cgroup']['counter_deltas']['io.stat']['value']['259:0']
        self.assertIsNone(reset['value']['rbytes'])
        sampler.previous['cgroup']['identity'] = {'device': 99, 'inode': 99}
        changed = sampler.sample(123)['cgroup']['counter_deltas']['io.stat']['value']['259:0']
        self.assertEqual(changed['error']['reason'], 'identity_changed')

    def test_disappearing_io_device_remains_unknown(self):
        sampler = ResourceSampler(**self.roots)
        sampler.sample(123)
        (self.cg / 'io.stat').write_text('')
        disappeared = sampler.sample(123)['cgroup']['counter_deltas']['io.stat']['value']['259:0']
        self.assertIsNone(disappeared['value'])
        self.assertEqual(disappeared['error']['reason'], 'missing_current')
        (self.cg / 'io.stat').write_text('259:0 rbytes=99999\n')
        reappeared = sampler.sample(123)['cgroup']['counter_deltas']['io.stat']['value']['259:0']
        self.assertIsNone(reappeared['value'])
        self.assertEqual(reappeared['error']['reason'], 'missing_previous')

    def test_monitor_refuses_existing_resource_evidence(self):
        evidence = self.root / 'resource-samples.jsonl'
        evidence.write_text('immutable resource evidence')
        script = Path(__file__).parent / 'monitor_memory.py'
        result = subprocess.run([sys.executable, '-B', str(script), '--out', str(self.root)],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('refusing to overwrite', result.stderr)
        self.assertEqual(evidence.read_text(), 'immutable resource evidence')

    def test_auto_binding_tracks_migration_without_silent_old_group(self):
        sampler = ResourceSampler(**self.roots)
        sampler.sample(123)
        (self.proc / '123/cgroup').write_text('0::/migrated.service\n')
        sample = sampler.sample(123)
        self.assertEqual(sample['binding']['actual_cgroup_path'], str(self.cgroot / 'migrated.service'))
        self.assertEqual(sample['cgroup']['path'], str(self.cgroot / 'migrated.service'))
        self.assertIsNone(sample['cgroup']['observations']['memory.current']['value'])

    def test_device_sector_units_and_partition_parent_not_added(self):
        path = self.root / 'model-placeholder'
        path.write_bytes(b'fixture')
        dev = path.stat().st_dev
        key = f'{os.major(dev)}:{os.minor(dev)}'
        disk = self.root / 'devices/nvme0n1'
        partition = disk / 'nvme0n1p1'
        partition.mkdir(parents=True)
        (disk / 'dev').write_text('259:0\n')
        (partition / 'partition').write_text('1\n')
        (partition / 'stat').write_text('1 0 7 2 3 0 11 4 1 6 7\n')
        (self.sysdev / key).symlink_to(partition, target_is_directory=True)
        selected = model_devices([path, path], self.sysdev)
        self.assertEqual(list(selected['devices']), [key])
        self.assertEqual(selected['devices'][key]['partition_parent_device']['value'], '259:0')
        sample = collect_resources(123, model_paths=[path], **self.roots)
        devices = sample['model_devices']['devices']
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[key]['stat']['value']['read_bytes'], 7 * 512)
        self.assertEqual(devices[key]['stat']['value']['write_bytes'], 11 * 512)
        self.assertNotIn('total', sample['model_devices'])
        sampler = ResourceSampler(model_paths=[path], **self.roots)
        sampler.sample(123)
        (partition / 'stat').write_text('2 0 9 2 3 0 11 4 0 6 7\n')
        delta = sampler.sample(123)['model_devices']['devices'][key]['counter_delta']
        self.assertEqual(delta['value']['read_bytes'], 1024)
        self.assertNotIn('ios_in_progress', delta['value'])

    def test_psi_uses_total_microseconds_and_bad_records_fail(self):
        parsed = pressure('some avg10=1.25 avg60=0.50 avg300=0.01 total=123\nfull avg10=0 avg60=0 avg300=0 total=0\n')
        self.assertEqual(parsed['some']['total'], 123)
        self.assertEqual(parsed['some']['avg10'], 1.25)
        with self.assertRaises(ValueError):
            pressure('some avg10=0\n')
        with self.assertRaises(ValueError):
            disk_counters('1 2 3')

    def test_monitor_lifecycle_sidecar_without_gpu(self):
        scripts = Path(__file__).parent
        out = self.root / 'monitor'
        ready = self.root / 'ready'
        pidfile = self.root / 'pid'
        # Stub only GPU querying. Real host /proc and cgroup observations run.
        code = ('import sys;sys.path.insert(0,sys.argv[1]);'
                'import monitor_memory as m;'
                'm.gpu_observation=lambda p:(None,"host_test_no_gpu");'
                'sys.argv=["monitor"]+sys.argv[2:];sys.exit(m.main())')
        monitor = subprocess.Popen([sys.executable, '-B', '-c', code, str(scripts),
                                    '--pid-file', str(pidfile), '--ready-file', str(ready),
                                    '--out', str(out), '--interval', '0.05', '--wait-timeout', '5'],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: monitor.kill() if monitor.poll() is None else None)
        deadline = time.monotonic() + 5
        while not ready.exists() and monitor.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(ready.exists())
        child = subprocess.Popen([sys.executable, '-B', '-c', 'import time;time.sleep(.3)'])
        self.addCleanup(lambda: child.kill() if child.poll() is None else None)
        pidfile.write_text(str(child.pid))
        child.wait(timeout=5)
        stdout, stderr = monitor.communicate(timeout=5)
        self.assertEqual(monitor.returncode, 0, (stdout, stderr))
        samples = [json.loads(line) for line in (out / 'resource-samples.jsonl').read_text().splitlines()]
        summary = json.loads((out / 'memory-peak.json').read_text())
        self.assertEqual(summary['stop_reason'], 'target_exited')
        self.assertEqual(samples[0]['phase'], 'before_start')
        self.assertEqual(samples[-1]['phase'], 'after_exit')
        self.assertEqual(summary['resource_observations']['sample_count'], len(samples))
        live = summary['resource_observations']['last_live_pid_sample']
        self.assertEqual(live['binding']['root_pid'], child.pid)
        self.assertIsNone(summary['service_total_physical_peak_bytes'])


if __name__ == '__main__':
    unittest.main()

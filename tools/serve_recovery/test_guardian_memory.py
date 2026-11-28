"""Native Guardian memory isolation with a real journal and CPU actor tree."""
import signal
import time

from tools.serve_recovery import control, test_runtime, test_version_native


class GuardianMemoryTests(test_runtime.GuardianFixture):
    def launch_observed(self, *, stalled=False):
        def version(service):
            service.config['journal'] = str(test_runtime.ROOT/
                'tests/fixtures/recovery/guardian_memory_journal.py')
            if stalled:
                (service.directory/'memory.stall').touch()
            return test_version_native.fixture(service)
        return self.launch(models=True, gateway=True,
            capacity_fingerprint='a'*64, version_fixture=version)

    def test_unavailable_first_sample_allows_stop_without_a_model_grant(self):
        service = self.launch_observed(stalled=True)
        service.wait(lambda s: (service.directory/'memory.entered').exists())
        current = control.request(service.service_id, 'status', timeout=.3)
        self.assertEqual(current['guardian_memory'], 'UNKNOWN')
        self.assertFalse(current['model_generation'])
        self.assertTrue(service.stop()['cleanup_confirmed'])
        service.assert_lease_free()

    def test_stalled_read_preserves_ready_control_and_discards_late_positive(self):
        service = self.launch_observed()
        first = service.wait(lambda s: s['business_ready'])
        stall = service.directory/'memory.stall'
        stall.touch()
        degraded = service.wait(lambda s: s['guardian_memory'] == 'UNKNOWN', timeout=6)
        self.assertEqual(degraded['state'], 'READY_DEGRADED')
        current = control.request(service.service_id, 'status', timeout=.3)
        self.assertTrue(current['business_ready'])
        self.assertEqual(current['model_pid'], first['model_pid'])
        error = service.directory/'memory.error'
        error.touch()
        stall.unlink()
        until = time.monotonic()+.5
        while time.monotonic() < until:
            self.assertEqual(service.status()['guardian_memory'], 'UNKNOWN')
            time.sleep(.01)
        error.unlink()
        restored = service.wait(lambda s: s['guardian_memory'] == 'OPEN' and s['business_ready'])
        for k in ('model_pid','model_start_ticks','controller_pid','guardian_pid'):
            self.assertEqual(restored[k], first[k])
        self.assertTrue(service.stop()['cleanup_confirmed'])
        service.assert_lease_free()

    def test_new_allocations_resume_after_cold_collector_recovers(self):
        service = self.launch_observed(stalled=True)
        service.wait(lambda s: (service.directory/'memory.entered').exists())
        (service.directory/'memory.low').touch()
        # Keep the initial read stalled beyond its freshness deadline.
        time.sleep(2.1)
        (service.directory/'memory.stall').unlink()
        held = service.wait(lambda s: s['guardian_memory'] == 'LOW')
        self.assertEqual(held['model_generation'], 0)
        (service.directory/'memory.low').unlink()
        ready = service.wait(lambda s: s['business_ready'])
        self.assertEqual(ready['model_generation'], 1)
        self.assertFalse(service.persistent()['attempts']['model'])
        self.assertTrue(service.stop()['cleanup_confirmed'])

    def test_version_switch_waits_for_independent_post_fault_headroom(self):
        service = self.launch_observed()
        first = service.wait(lambda s: s['business_ready'])
        (service.directory/'memory.stall').touch()
        service.wait(lambda s: (service.directory/'memory.entered').exists() and
                              s['guardian_memory'] == 'OPEN')
        test_runtime.signal_identity(first['controller_pid'],
                                     first['controller_start_ticks'], signal.SIGABRT)
        service.wait(lambda s: not s['business_ready'])
        (service.directory/'memory.low').touch()
        (service.directory/'memory.stall').unlink()
        rejected = service.wait(lambda s: (s.get('version_exit_observation') or {}).get('role') == 'controller')
        self.assertFalse(rejected['version_exit_observation']['accepted'])
        self.assertFalse(rejected['version_exit_observation']['resources_safe'])
        self.assertEqual(rejected['version_recovery']['probe_count'], 0)
        (service.directory/'memory.low').unlink()
        restored = service.wait(lambda s: s['business_ready'], timeout=20)
        self.assertEqual(restored['version_recovery']['selected'], 'primary')
        self.assertTrue(service.stop()['cleanup_confirmed'])

    def test_memory_returned_after_exit_cannot_replace_low_pre_fault_witness(self):
        service = self.launch_observed()
        first = service.wait(lambda s: s['business_ready'])
        low = service.directory/'memory.low'
        low.touch()
        service.wait(lambda s: s['guardian_memory'] == 'LOW')
        test_runtime.signal_identity(first['controller_pid'],
                                     first['controller_start_ticks'], signal.SIGABRT)
        rejected = service.wait(lambda s: (s.get('version_exit_observation') or {}).get('role')=='controller')
        self.assertFalse(rejected['version_exit_observation']['accepted'])
        self.assertFalse(rejected['version_exit_observation']['resources_safe'])
        low.unlink()
        restored = service.wait(lambda s: s['business_ready'] and s['model_generation'] > first['model_generation'],timeout=20)
        self.assertEqual(restored['version_recovery']['selected'], 'primary')
        self.assertTrue(service.stop()['cleanup_confirmed'])
        service.assert_lease_free()

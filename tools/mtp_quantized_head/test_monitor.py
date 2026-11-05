"""CPU regressions for success returned before a sampler's late failure."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

import stage


class FinalMonitorTest(unittest.TestCase):
    def setUp(self):
        parent = stage.ROOT / '.q4t-work/monitor-contracts'
        parent.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=parent)
        self.base = Path(self.temporary.name)
        self.output = self.base / 'run'
        self.output.mkdir()
        self.plan = dict(output=str(self.output))
        self.state = dict(status='MEASURED_REQUIRES_REVIEW', monitor_failures=[])
        self.summary = dict(sampler_status='STOPPED', sampler_failure=None,
            failures_at_exit=[], unflushed_attempts=0, sample_attempts=10,
            flushed_samples=10, exit_pending=dict(active=0),
            last_flushed_heartbeat=12, final_successful_cycle_duration_seconds=1)

    def tearDown(self):
        self.temporary.cleanup()

    def freeze(self):
        for name, record in [('state.json', self.state),
                             ('telemetry-summary.json', self.summary)]:
            (self.output / name).write_text(json.dumps(record))
        post = dict(status='MEASURED_REQUIRES_REVIEW', final_monitor_errors=[],
            monitor_failures=[], cleanup_errors=[],
            final_monitor_barrier=dict(boundary_monotonic=10),
            state_sha256=stage.sha(self.output / 'state.json'),
            final_summary_sha256=stage.sha(self.output / 'telemetry-summary.json'))
        (self.base / 'postflight.json').write_text(json.dumps(post))

    def reject(self):
        with self.assertRaises(stage.legacy.EnvironmentAbort):
            stage.require_complete_monitor(self.plan)

    def test_complete_post_shutdown_sample(self):
        self.freeze()
        stage.require_complete_monitor(self.plan)

    def test_original_success_state_cannot_hide_join_failure(self):
        self.state['monitor_failures'] = [
            'ENV_ABORT telemetry stopped with unresolved exit identities']
        self.summary['sampler_status'] = 'ENV_ABORT'
        self.summary['failures_at_exit'] = copy.copy(self.state['monitor_failures'])
        self.summary['exit_pending']['active'] = 1
        self.freeze()
        self.reject()

    def test_missing_barrier_is_not_success(self):
        self.freeze()
        (self.base / 'postflight.json').unlink()
        self.reject()

    def test_late_failure_even_if_legacy_state_looks_clean(self):
        self.summary['failures_at_exit'] = ['sampler pending cleanup failed']
        self.freeze()
        self.reject()

    def test_pending_identity_is_not_complete_sample(self):
        self.summary['exit_pending']['active'] = 1
        self.freeze()
        self.reject()

    def test_stale_sample_cannot_satisfy_shutdown_barrier(self):
        self.summary['last_flushed_heartbeat'] = 10
        self.freeze()
        self.reject()

    def test_changed_final_receipt_is_rejected(self):
        self.freeze()
        self.summary['flushed_samples'] = 9
        (self.output / 'telemetry-summary.json').write_text(json.dumps(self.summary))
        self.reject()


if __name__ == '__main__':
    unittest.main()

"""Auxiliary eviction is healthy; actual failures and incomplete cleanup are not."""
from copy import deepcopy
import unittest

from tools.serve_live.checks import speech_health


class Health(unittest.TestCase):
    def setUp(self):
        common = dict(reason='none', core_readiness_dependency=False,
                      load_attempts=2, load_failures=0, compute_failures=0,
                      recovery='request_driven', asset_cleanup_pending=False)
        self.config = dict(
            native_asr_health=dict(common, state='metadata_ready', device_bytes=0),
            native_tts_health=dict(common, state='ready', device_bytes=8123997956))

    def reject(self, change):
        for name in self.config:
            candidate = deepcopy(self.config)
            change(candidate[name])
            with self.assertRaises(RuntimeError):
                speech_health(candidate)

    def test_ready_and_released_families_are_healthy(self):
        result = speech_health(self.config)
        self.assertEqual(result, self.config)
        self.config['native_asr_health'], self.config['native_tts_health'] = (
            self.config['native_tts_health'], self.config['native_asr_health'])
        self.assertEqual(speech_health(self.config), self.config)

    def test_unavailable_or_loading_state_cannot_pass(self):
        for state in ('disabled', 'loading', 'configured', 'degraded', ''):
            self.reject(lambda health: health.update(state=state))

    def test_failures_or_unreleased_cleanup_cannot_pass(self):
        for field, value in (('reason', 'gpu_unhealthy'), ('load_failures', 1),
                             ('compute_failures', 1),
                             ('asset_cleanup_pending', True),
                             ('core_readiness_dependency', True),
                             ('recovery', 'automatic_model_restart')):
            self.reject(lambda health: health.update({field: value}))

    def test_inconsistent_residency_cannot_pass(self):
        for state, size in (('ready', 0), ('metadata_ready', 1)):
            self.reject(lambda health: health.update(state=state, device_bytes=size))

    def test_missing_or_invalid_counters_cannot_pass(self):
        for field in ('device_bytes', 'load_attempts', 'load_failures',
                      'compute_failures'):
            self.reject(lambda health: health.pop(field))
            for value in (-1, True, None, 1.5, '0'):
                self.reject(lambda health: health.update({field: value}))
        self.reject(lambda health: health.update(load_attempts=0))


if __name__ == '__main__':
    unittest.main()

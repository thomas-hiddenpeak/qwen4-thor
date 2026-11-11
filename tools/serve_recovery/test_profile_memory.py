"""Pressure, cold capacity and recovery use only fresh actual RAM evidence."""
import unittest
import uuid

from tools.serve_recovery import ledger, profile_memory as m, profile_policy as p
from tools.serve_recovery import test_profile_policy as fixtures


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.registry = fixtures.registry()
        self.state = p.initial(self.registry, ledger.Stamp(str(uuid.uuid4()), 0))
        self.detector = m.Detector(self.registry, p.DEFAULTS)

    def sample(self, seconds, free=None, state='OPEN', **changes):
        value = dict(total_bytes=128*1024**3,
            available_bytes=7*1024**3 if free is None else free,
            sampled_monotonic=seconds, state=state)
        value.update(changes)
        return value

    def test_cold_selection_requires_actual_qualified_capacity(self):
        needed = self.registry['qualifications']['primary']['startup_required_bytes']
        self.assertEqual(m.cold(self.registry, self.state, self.sample(0, needed)),
                         dict(ready=True, target='primary', reason='qualified_capacity'))
        self.assertEqual(m.cold(self.registry, self.state, self.sample(0, needed-1)),
                         dict(ready=False, target='light', reason='cold_memory_capacity'))
        self.assertEqual(m.cold(self.registry, self.state, self.sample(0, needed-2))
                         ['reason'], 'memory_headroom')
        for value in [None, {}, self.sample(0, True), self.sample(0, -1)]:
            self.assertEqual(m.cold(self.registry, self.state, value)['reason'], 'memory_unknown')
        prepared = p.prepare(self.state, 'light', 'cold_memory_capacity',
            str(uuid.uuid4()), 0, ledger.Stamp(self.state['clock']['boot_id'], 0))
        # Even abundant RAM cannot cancel a committed pending light intent.
        self.assertEqual(m.cold(self.registry, prepared, self.sample(0, needed))
                         ['target'], 'light')

    def test_cached_unknown_and_stale_observations_never_accumulate_pressure(self):
        d = self.detector
        for second in range(30):
            self.assertIsNone(d.assess('primary', self.sample(second), second*ledger.SECOND))
        self.assertIsNone(d.assess('primary', self.sample(29), 30*ledger.SECOND))
        self.assertIsNone(d.assess('primary', self.sample(30, state='UNKNOWN'), 30*ledger.SECOND))
        for second in range(31, 61):
            self.assertIsNone(d.assess('primary', self.sample(second), second*ledger.SECOND))
        self.assertEqual(d.assess('primary', self.sample(61), 61*ledger.SECOND),
            dict(target='light', reason='sustained_memory_pressure'))
        self.assertIsNone(d.assess('primary', self.sample(61), 64*ledger.SECOND))
        self.assertIsNone(d.since)
        self.assertIsNone(d.assess('primary', self.sample(100), 100*ledger.SECOND))

    def test_promotion_requires_extra_allocation_headroom_and_continuous_high_water(self):
        d = self.detector
        for second in range(130):
            self.assertIsNone(d.assess('light', self.sample(second, d.high-1),
                                      second*ledger.SECOND))
        for second in range(130, 250):
            self.assertIsNone(d.assess('light', self.sample(second, d.high),
                                      second*ledger.SECOND))
        self.assertEqual(d.assess('light', self.sample(250, d.high), 250*ledger.SECOND),
                         dict(target='primary', reason='sustained_memory_recovery'))
        self.assertIsNone(d.assess('light', self.sample(251, d.high),
                                  251*ledger.SECOND, ready=False))
        self.assertIsNone(d.since)

    def test_invalid_future_and_interrupted_samples_reset_evidence(self):
        for bad in [self.sample(30), self.sample(float('nan')), self.sample(True),
                    self.sample(0, total_bytes=1), self.sample(0, state='TRIPPED')]:
            self.detector.assess('primary', self.sample(0), 0)
            self.assertIsNone(self.detector.assess('primary', bad, ledger.SECOND))
            self.assertIsNone(self.detector.since)
        d = self.detector
        d.assess('primary', self.sample(0), 0)
        self.assertIsNone(d.assess('primary', self.sample(4), 4*ledger.SECOND))
        self.assertEqual(d.since, 4*ledger.SECOND)


if __name__ == '__main__': unittest.main()

"""Deterministic timing/parsing/ownership checks, no GPU or HTTP."""
import os
import signal
import threading
import warnings
import unittest

from tools.serve_service import memory, package


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.value = dict(total_bytes=8*1024**3, available_bytes=4*1024**3)
        self.sent = []
        self.watch = memory.Watchdog(package.settings(8080, 3),
            lambda owner: self.sent.append(owner) or True,
            reader=lambda: self.value, now=lambda: self.now)

    def sample(self, when, available):
        self.now = when
        self.value = dict(total_bytes=8*1024**3, available_bytes=available)
        self.watch.sample()

    def test_parser_rejects_invalid_and_oversized_kernel_observations(self):
        good = 'MemTotal: 100 kB\nMemAvailable: 20 kB\n'
        self.assertEqual(memory.parse(good)['available_bytes'], 20*1024)
        for raw in (good[:-1], good+good, good.replace('20', '101'),
                    good.replace('100', '0'), good.replace('20', '-1'),
                    good.replace('kB', 'MB'), good.replace('20', '２０'),
                    'MemTotal: 100 kB\n', good+' '*16384):
            with self.subTest(raw=raw[:80]), self.assertRaises(ValueError):
                memory.parse(raw)

    def test_transient_low_recovers_and_resets_continuous_deadline(self):
        self.sample(0, 0)
        self.sample(1.99, 0)
        self.sample(2, 1024**3)  # Equality is safe.
        self.sample(3, 0)
        self.sample(4.99, 0)
        self.watch.check()
        self.sample(5, 0)
        with self.assertRaisesRegex(RuntimeError, 'critical_memory_pressure'):
            self.watch.check()

    def test_runtime_unknown_never_signals_and_recovers_in_place(self):
        self.watch.attach(dict(pid=42))
        self.sample(0, None)
        self.sample(4.99, None)
        self.watch.check()
        self.sample(500, None)
        self.assertEqual(self.watch.snapshot()['reason'],
                         'memory_observation_unavailable')
        self.assertFalse(self.watch.tripped.is_set())
        self.assertFalse(self.sent)
        self.sample(501, 4*1024**3)
        self.assertEqual(self.watch.snapshot()['state'], 'OPEN')
        self.assertIsNone(self.watch.snapshot()['reason'])
        self.watch.check()

    def test_unknown_cannot_substitute_for_confirmed_low_pressure(self):
        for when in range(6):
            self.sample(when, 0 if when % 2 else None)
        self.assertFalse(self.watch.tripped.is_set())
        self.sample(7, 0)
        self.assertTrue(self.watch.tripped.is_set())
        self.assertEqual(self.watch.snapshot()['reason'], 'critical_memory_pressure')
        self.sample(500, None)
        self.assertEqual(self.watch.snapshot()['reason'], 'critical_memory_pressure')

    def test_unobserved_interval_cannot_earn_critical_pressure_time(self):
        self.watch.attach(dict(pid=42))
        for when in (0, 300, 300, 301):
            self.sample(when, 0)
            self.assertFalse(self.sent)
        self.sample(302, 0)
        self.assertEqual(self.sent, [dict(pid=42)])
        self.assertEqual(self.watch.snapshot()['reason'], 'critical_memory_pressure')

    def test_delayed_low_result_is_unknown_and_cannot_complete_a_streak(self):
        self.sample(0, 0)
        def delayed():
            self.now += 300
            return {'total_bytes': 8*1024**3, 'available_bytes': 0}
        self.watch.reader = delayed
        self.watch.sample()
        self.assertEqual(self.watch.snapshot()['state'], 'UNKNOWN')
        self.assertFalse(self.watch.tripped.is_set())
        self.watch.reader = lambda: self.value
        self.sample(301, 0)
        self.assertEqual(self.watch.snapshot()['state'], 'LOW')
        self.sample(303, 0)
        self.assertTrue(self.watch.tripped.is_set())

    def test_stalled_reader_is_bounded_across_observers_and_drops_late_result(self):
        entered, release, inspected = (threading.Event() for _ in range(3))
        calls = []
        def stalled():
            calls.append(1)
            entered.set()
            release.wait(3)
            return dict(total_bytes=8*1024**3, available_bytes=0)
        self.watch.cfg.update(memory_poll_seconds=.25, memory_unknown_seconds=.05)
        self.watch.reader = stalled
        sampler = threading.Thread(target=self.watch.sample)
        sampler.start()
        def inspect():
            self.watch.snapshot()
            self.watch.attach(dict(pid=42))
            inspected.set()
        inspect_thread = threading.Thread(target=inspect)
        try:
            self.assertTrue(entered.wait(1))
            inspect_thread.start()
            self.assertTrue(inspected.wait(.15))
            other = memory.Watchdog(self.watch.cfg, self.sent.append, reader=stalled)
            with self.assertRaisesRegex(RuntimeError, 'memory_observation_unavailable'):
                other.start()
            sampler.join(1)
            self.assertFalse(sampler.is_alive())
            self.assertEqual(self.watch.snapshot()['state'], 'UNKNOWN')
            self.assertEqual(calls, [1])
            self.watch.close()
        finally:
            release.set()
            sampler.join(2)
            if inspect_thread.ident is not None: inspect_thread.join(2)
        # A new sample waits for the old read to retire. Its late low value
        # cannot trip the closed watchdog or be served to another instance.
        fresh = memory.Watchdog(self.watch.cfg, self.sent.append, reader=lambda: self.value)
        fresh.sample(startup=True)
        self.assertEqual(fresh.snapshot()['state'], 'OPEN')
        self.assertFalse(self.watch.tripped.is_set())
        self.assertFalse(self.sent)

    def test_forked_controller_gets_its_own_reader(self):
        self.sample(0, 4*1024**3)
        with warnings.catch_warnings():
            # Exercise the Linux fork boundary in a process with a live sampler.
            warnings.simplefilter('ignore', DeprecationWarning)
            pid = os.fork()
        if pid == 0:
            try:
                signal.alarm(2)
                cfg = dict(self.watch.cfg, memory_unknown_seconds=.25)
                child = memory.Watchdog(cfg, self.sent.append, reader=lambda: self.value)
                child.sample(startup=True)
                os._exit(0 if child.snapshot()['state'] == 'OPEN' else 1)
            except BaseException:
                os._exit(2)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0)

    def test_reader_exit_releases_slot_for_next_observation(self):
        def exiting():
            raise SystemExit(1)
        self.watch.reader = exiting
        self.watch.sample()
        self.assertEqual(self.watch.snapshot()['state'], 'UNKNOWN')
        self.watch.reader = lambda: self.value
        self.watch.sample()
        self.assertEqual(self.watch.snapshot()['state'], 'OPEN')
        self.assertFalse(self.sent)

    def test_startup_unknown_still_refuses_new_allocation(self):
        self.value = dict(total_bytes=8*1024**3, available_bytes=None)
        self.watch.sample(startup=True)
        with self.assertRaisesRegex(RuntimeError, 'memory_observation_unavailable'):
            self.watch.check()
        self.assertFalse(self.sent)

    def test_trip_before_attachment_signals_late_owned_child_only_once(self):
        self.sample(0, 0)
        self.sample(2, 0)
        self.assertFalse(self.sent)
        owner = dict(pid=42, start_ticks=123, boot_id='fixture')
        self.watch.attach(owner)
        self.watch.attach(owner)
        self.sample(3, 0)
        self.assertEqual(self.sent, [owner])
        self.assertTrue(self.watch.snapshot()['signal_sent'])

    def test_closed_watchdog_cannot_signal_a_new_owner(self):
        self.sample(0, 0)
        self.sample(2, 0)
        self.watch.close()
        self.watch.attach(dict(pid=99))
        self.assertFalse(self.sent)

    def test_detached_old_process_is_never_notified(self):
        self.watch.attach(dict(pid=42))
        self.watch.attach(None)
        self.sample(0, 0)
        self.sample(2, 0)
        self.assertFalse(self.sent)

    def test_signal_failure_retains_protective_cause(self):
        def fail(_):
            raise OSError('fixed pidfd failure')
        self.watch.notify = fail
        self.watch.attach(dict(pid=42))
        self.sample(0, 0)
        self.sample(2, 0)
        value = self.watch.snapshot()
        self.assertEqual(value['reason'], 'critical_memory_pressure')
        self.assertIn('fixed pidfd failure', value['signal_error'])
        self.assertFalse(value['signal_sent'])


if __name__ == '__main__':
    unittest.main()

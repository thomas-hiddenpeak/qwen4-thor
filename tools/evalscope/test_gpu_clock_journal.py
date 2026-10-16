"""CPU fake-IO recovery tests, including interrupted hardware writes."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import gpu_clock_journal as journal


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[2] / '.q4t-work/clock-journal-tests'
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'lease.recovery-required'
        self.values = [1575, 1575]
        self.writes = []
        self.identity = {'boot_id': 'fake-boot', 'device': 'fake-device'}

    def write(self, field, value):
        state = json.loads(self.path.read_text())
        self.assertEqual(state['pending']['field'], field)
        self.assertEqual(state['pending']['value'], value)
        self.values[0 if field == 'min_freq' else 1] = value
        self.writes.append((field, value))
        self.assertLessEqual(*self.values)

    def controller(self, write=None, identity=None):
        return journal.ClockJournal(self.path, lambda: tuple(self.values),
                                    write or self.write, identity or self.identity,
                                    [1260, 1575])

    def prepare(self):
        c = self.controller()
        c.prepare([1575, 1575], [1260, 1260])
        return c

    def test_normal_independent_restore(self):
        self.prepare().apply()
        self.assertTrue(self.path.exists())
        self.controller().restore()
        self.assertEqual(self.values, [1575, 1575])
        self.assertFalse(self.path.exists())

    def test_prepare_without_write_recovers(self):
        self.prepare()
        self.controller().restore()
        self.assertFalse(self.path.exists())
        self.assertEqual(self.writes, [])

    def test_interrupted_each_apply_or_restore_write(self):
        for restoring in (False, True):
            for index in (1, 2):
                for after_effect in (False, True):
                    with self.subTest(restoring=restoring, index=index,
                                      after_effect=after_effect):
                        self.values[:] = [1575, 1575]
                        c = self.prepare()
                        if restoring:
                            c.apply()
                        calls = 0
                        def interrupted(field, value):
                            nonlocal calls
                            calls += 1
                            if calls == index and not after_effect:
                                raise KeyboardInterrupt('before effect')
                            self.write(field, value)
                            if calls == index:
                                raise KeyboardInterrupt('after effect')
                        c.write = interrupted
                        with self.assertRaises(KeyboardInterrupt):
                            c.restore() if restoring else c.apply()
                        self.assertTrue(self.path.exists())
                        self.controller().restore()
                        self.assertEqual(self.values, [1575, 1575])
                        self.assertFalse(self.path.exists())

    def test_foreign_change_keeps_marker_without_writes(self):
        self.prepare().apply()
        self.values[:] = [1575, 1575]
        before = len(self.writes)
        with self.assertRaises(RuntimeError):
            self.controller().restore()
        self.assertTrue(self.path.exists())
        self.assertEqual(len(self.writes), before)

    def test_changed_identity_and_corrupt_journal_stay_closed(self):
        self.prepare()
        with self.assertRaises(RuntimeError):
            self.controller(identity={'boot_id': 'different'}).restore()
        self.path.write_text('{broken')
        with self.assertRaises(ValueError):
            self.controller().restore()
        self.assertTrue(self.path.exists())
        self.assertEqual(self.writes, [])

    def test_existing_marker_is_not_overwritten(self):
        self.path.write_text('previous recovery')
        with self.assertRaises(FileExistsError):
            self.prepare()
        self.assertEqual(self.path.read_text(), 'previous recovery')
        self.assertEqual(self.writes, [])

    def test_journal_persist_failure_prevents_hardware_write(self):
        c = self.prepare()
        with patch.object(journal, 'persist', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                c.apply()
        self.assertEqual(self.writes, [])
        self.assertTrue(self.path.exists())
        self.controller().restore()

    def test_persist_failure_after_hardware_effect_recovers(self):
        c = self.prepare()
        real_persist = journal.persist
        calls = 0
        def fail_after_effect(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError('failed to record completed hardware write')
            return real_persist(*args, **kwargs)
        with patch.object(journal, 'persist', side_effect=fail_after_effect):
            with self.assertRaises(OSError):
                c.apply()
        self.assertEqual(self.values, [1260, 1575])
        self.controller().restore()
        self.assertEqual(self.values, [1575, 1575])
        self.assertFalse(self.path.exists())

    def test_ignored_write_can_be_recovered(self):
        c = self.prepare()
        c.write = lambda *args: None
        with self.assertRaises(RuntimeError):
            c.apply()
        self.controller().restore()
        self.assertFalse(self.path.exists())


if __name__ == '__main__':
    unittest.main()

"""Fake-file tests only: never access sysfs or start GPU work."""
import unittest
from gpu_clock_range import ClockRange, steps


class TransactionTests(unittest.TestCase):
    def setup_io(self):
        self.values = {'min_freq': 1575, 'max_freq': 1575}
        self.writes = []
        def read(): return self.values['min_freq'], self.values['max_freq']
        def write(name, value):
            self.writes.append((name, value)); self.values[name] = value
            assert self.values['min_freq'] <= self.values['max_freq']
        self.read, self.write = read, write
        return ClockRange(read, write, (1575, 1575), [1260, 1575])

    def test_lower_then_restore_exact_order(self):
        c = self.setup_io(); c.apply((1260,1260)); c.restore()
        self.assertEqual(self.writes, [('min_freq',1260),('max_freq',1260),
                                      ('max_freq',1575),('min_freq',1575)])
        self.assertEqual(self.read(), (1575,1575))

    def test_noop_has_no_writes(self):
        c = self.setup_io(); c.restore(); self.assertEqual(self.writes, [])

    def test_unsupported_and_above_original(self):
        c = self.setup_io()
        with self.assertRaises(ValueError): c.apply((1400,1400))
        c.available.add(1800)
        with self.assertRaises(ValueError): c.apply((1800,1800))
        self.assertEqual(self.writes, [])

    def test_second_write_failure_restores_partial_range(self):
        c = self.setup_io()
        def fail(name,value):
            if name=='max_freq' and value==1260: raise OSError('write failed')
            self.write(name,value)
        c.write=fail
        with self.assertRaises(OSError): c.apply((1260,1260))
        c.restore(); self.assertEqual(self.read(),(1575,1575))

    def test_error_after_effect_restores(self):
        c = self.setup_io()
        def fail(name,value):
            self.write(name,value)
            if name=='max_freq' and value==1260: raise OSError('after effect')
        c.write=fail
        with self.assertRaises(OSError): c.apply((1260,1260))
        c.restore(); self.assertEqual(self.read(),(1575,1575))

    def test_ignored_write_rejected(self):
        c = self.setup_io(); c.write=lambda *args: None
        with self.assertRaises(RuntimeError): c.apply((1260,1260))
        self.assertEqual(self.read(),(1575,1575))

    def test_foreign_change_not_overwritten(self):
        c = self.setup_io(); c.apply((1260,1260)); self.values['max_freq']=1400
        count=len(self.writes)
        with self.assertRaises(RuntimeError): c.restore()
        self.assertEqual(len(self.writes),count)

    def test_initial_identity_mismatch(self):
        with self.assertRaises(RuntimeError):
            ClockRange(lambda:(100,200),lambda *a:None,(200,200),[100,200])

    def test_invalid_ranges(self):
        for current,target in [((0,1),(1,1)),((2,1),(1,1)),((1,1),(2,1))]:
            with self.assertRaises(ValueError): steps(current,target)


if __name__=='__main__': unittest.main()

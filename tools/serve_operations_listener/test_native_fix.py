"""Repair gate tests with no HTTP/model work."""
import unittest
from unittest.mock import patch
from tools.serve_operations_listener import native_fix as f

class Repair(unittest.TestCase):
    def test_original_timeout_did_not_start_any_model_or_generation(self):
        f.prior_gate()
        read=f.cpu.read
        def changed(path):
            value=read(path)
            if str(path).endswith('business/result.json'):
                value['counts']['starts']=1
            return value
        with patch.object(f.cpu,'read',side_effect=changed),self.assertRaises(RuntimeError):f.prior_gate()

    def test_fixture_uses_repeatable_hooks_without_changing_production(self):
        built=f.cpu.read(f.s.BASE/'build-native-fix/result.json')
        plan=f.cpu.read(f.ORIGINAL_PACKAGE/'plan.json')
        self.assertTrue(built['passed'])
        self.assertEqual(built['production_sha256'],plan['candidate_sha256'])
        self.assertNotEqual(f.PACKAGE,f.ORIGINAL_PACKAGE)
        self.assertNotEqual(f.OUTPUT,f.FAILED)

if __name__=='__main__':unittest.main()

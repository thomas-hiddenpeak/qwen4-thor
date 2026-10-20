import unittest
from cache_start import check_vector

class CacheGateTest(unittest.TestCase):
    def test_exact(self):
        expected={'a.bin':4096,'b.safetensors':0}
        got={'complete_file_set_observed':True,'files':[{'path':p,'resident_bytes':n} for p,n in expected.items()]}
        self.assertEqual(check_vector(got,expected),expected)
    def test_extra_inherited(self):
        got={'complete_file_set_observed':True,'files':[{'path':'a.bin','resident_bytes':8192}]}
        with self.assertRaises(RuntimeError):check_vector(got,{'a.bin':4096})
    def test_missing_page(self):
        got={'complete_file_set_observed':True,'files':[{'path':'a.bin','resident_bytes':0}]}
        with self.assertRaises(RuntimeError):check_vector(got,{'a.bin':4096})
    def test_unknown(self):
        with self.assertRaises(RuntimeError):check_vector({'complete_file_set_observed':False}, {})
    def test_unexpected_file(self):
        got={'complete_file_set_observed':True,'files':[{'path':'a.bin','resident_bytes':4096},{'path':'b.bin','resident_bytes':4096}]}
        with self.assertRaises(RuntimeError):check_vector(got,{'a.bin':4096})

if __name__=='__main__':unittest.main()

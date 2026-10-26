"""CPU-only negative evidence tests, executed after B's first HTTP gate."""
import unittest
from run_stage import audit_stats

class LazyAudit(unittest.TestCase):
    def setUp(self):
        self.row=dict(queries='5',hits='1',no_match='2',incomplete='0',d0_mismatch='0',invalid='0',exhausted='0',
          numeric_checks='2',classified_tokens='64',numeric_rejected='1',short_rejected='1',classify_failed='0',
          cache_decodes='2',cache_lookups='64',cache_allocations='1',cache_bytes='248320',peak_bytes='1884160',
          numeric_ns='5',query_ns='10',accepted='0,0,0,1',neural_accepted='1,1,1,1')
    def test_valid(self):self.assertEqual(audit_stats(self.row)['hits'],1)
    def test_short_requests_do_not_classify(self):
        r=dict(self.row,queries='1',hits='0',no_match='0',numeric_checks='0',classified_tokens='0',numeric_rejected='0',
          cache_decodes='0',cache_lookups='0',cache_allocations='0',cache_bytes='0',numeric_ns='0',accepted='0,0,0,0',neural_accepted='1,0,0,0')
        self.assertEqual(audit_stats(r)['cache_bytes'],0)
    def test_eager_counter_rule_rejected(self):
        with self.assertRaises(RuntimeError):audit_stats(dict(self.row,numeric_checks='3'))
    def test_failed_classifier_cannot_pass(self):
        with self.assertRaises(RuntimeError):audit_stats(dict(self.row,classify_failed='1'))
    def test_resource_bounds(self):
        for key,value in [('peak_bytes','2097153'),('cache_bytes','262145'),('cache_allocations','2')]:
            with self.subTest(key=key),self.assertRaises(RuntimeError):audit_stats(dict(self.row,**{key:value}))
    def test_lookup_and_decode_accounting(self):
        for key,value in [('cache_lookups','63'),('cache_decodes','65'),('classified_tokens','129'),('numeric_ns','11')]:
            with self.subTest(key=key),self.assertRaises(RuntimeError):audit_stats(dict(self.row,**{key:value}))
    def test_histogram_integrity(self):
        with self.assertRaises(RuntimeError):audit_stats(dict(self.row,accepted='0,0,0,2'))
    def test_missing_counter_is_failure(self):
        del self.row['classify_failed']
        with self.assertRaises(KeyError):audit_stats(self.row)

if __name__=='__main__':unittest.main(verbosity=2)

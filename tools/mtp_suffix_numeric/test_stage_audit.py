"""Synthetic evidence contracts. Run only after the first real HTTP gate."""
import json
from pathlib import Path
import tempfile
import unittest

from run_stage import ROOT, audit_server


class NumericStageAudit(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='numeric-audit-',dir=ROOT/'.q4t-work')
        self.output = Path(self.temp.name)
        (self.output/'exit.json').write_text(json.dumps(
            {'server':0,'http_output_checks_passed':True}))
        self.row = ('[suffix] request=one queries=10 hits=3 invalid=0 exhausted=0 '
                    'peak_bytes=1884160 query_ns=1000 numeric_checks=5 '
                    'classified_tokens=23 numeric_rejected=2 numeric_ns=100\n')
        self.table = ('[suffix-classes] event=init bytes=248320 cap_bytes=262144 '
                      'unknown=243 init_ns=1000\n'
                      '[q4t] suffix enabled: k=3 numeric_gate=1\n'
                      '[suffix-classes] event=release bytes=248320 release_ns=100\n')

    def tearDown(self):
        self.temp.cleanup()

    def write(self,row=None,table=None,enabled=True,extra=''):
        text = ('[request] event=admitted id=one\n' +
                (self.table if table is None else table) +
                (self.row if row is None else row) + extra +
                '[startup-reservation] {"event":"finalized","status":"PASS",'
                '"release_unknown":0}\n')
        (self.output/'server.log').write_text(text)
        (self.output/'server-command.json').write_text(json.dumps(
            {'argv':['q4t','serve']+(['--mtp-suffix'] if enabled else [])}))

    def test_records_nested_timing_and_exact_table_lifecycle(self):
        self.write()
        audit_server(self.output,1)
        record = json.loads((self.output/'numeric-audit.json').read_text())
        self.assertEqual(record['table_bytes'],248320)
        self.assertEqual(record['requests'][0]['numeric_rejected'],'2')
        self.assertIn('included',record['cost_scope'])

    def test_zero_queries_remain_valid_with_real_allocations(self):
        row = self.row
        for old,new in [('queries=10','queries=0'),('hits=3','hits=0'),
                        ('numeric_checks=5','numeric_checks=0'),
                        ('classified_tokens=23','classified_tokens=0'),
                        ('numeric_rejected=2','numeric_rejected=0'),
                        ('numeric_ns=100','numeric_ns=0')]:
            row = row.replace(old,new)
        self.write(row=row)
        audit_server(self.output,1)

    def test_rejects_ungated_binary_and_missing_or_duplicate_release(self):
        for table in ['',self.table.replace(' numeric_gate=1',''),
                      '\n'.join(self.table.splitlines()[:-1])+'\n',self.table+self.table]:
            with self.subTest(table=table),self.assertRaises(RuntimeError):
                self.write(table=table)
                audit_server(self.output,1)

    def test_rejects_wrong_capacity_domain_or_zero_index_allocation(self):
        for old,new in [('bytes=248320','bytes=65536'),('cap_bytes=262144','cap_bytes=999999'),
                        ('unknown=243','unknown=0')]:
            with self.subTest(old=old),self.assertRaises(RuntimeError):
                self.write(table=self.table.replace(old,new))
                audit_server(self.output,1)
        with self.assertRaises(RuntimeError):
            self.write(row=self.row.replace('peak_bytes=1884160','peak_bytes=0'))
            audit_server(self.output,1)

    def test_rejection_accounting_and_nested_cost_must_close(self):
        for old,new in [('numeric_checks=5','numeric_checks=6'),
                        ('classified_tokens=23','classified_tokens=14'),
                        ('numeric_rejected=2','numeric_rejected=-1'),
                        ('numeric_ns=100','numeric_ns=1001'),
                        ('numeric_checks=5 ','')]:
            with self.subTest(old=old),self.assertRaises(RuntimeError):
                self.write(row=self.row.replace(old,new))
                audit_server(self.output,1)

    def test_suffix_off_must_not_construct_table(self):
        self.write(row='',table='',enabled=False)
        audit_server(self.output,1)
        with self.assertRaises(RuntimeError):
            self.write(row='',enabled=False)
            audit_server(self.output,1)

    def test_explicit_fallback_never_passes_with_healthy_summary(self):
        for marker in ['MTP trunk alloc failed','MTP load failed','plain decode for this request']:
            with self.subTest(marker=marker),self.assertRaises(RuntimeError):
                self.write(extra=marker+'\n')
                audit_server(self.output,1)


if __name__=='__main__':
    unittest.main()

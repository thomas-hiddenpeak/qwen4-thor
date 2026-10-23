"""Synthetic evidence contracts; execute only after the first real HTTP gate."""
import json
from pathlib import Path
import tempfile
import unittest

from run_stage import ROOT, audit_server


class AdmissionStageAudit(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='admission-audit-',dir=ROOT/'.q4t-work')
        self.output = Path(self.temp.name)
        (self.output/'exit.json').write_text(json.dumps(
            {'server':0,'http_output_checks_passed':True}))
        self.values = dict(request='one',queries=10,hits=3,no_match=1,incomplete=1,
            d0_mismatch=2,invalid=0,exhausted=0,peak_bytes=1884160,query_ns=1000,
            numeric_checks=6,classified_tokens=150,numeric_rejected=2,
            short_rejected=1,numeric_ns=100)
        self.init = ('[suffix-classes] event=init bytes=248320 cap_bytes=262144 '
                     'unknown=243 init_ns=1000\n')
        self.banner = '[q4t] suffix enabled: k=3 numeric_gate=1 min_admitted_match=32\n'
        self.release = '[suffix-classes] event=release bytes=248320 release_ns=100\n'
        self.end = '[q4t][request] id=one event=end stage=response released=1 outcome=success\n'

    def tearDown(self):
        self.temp.cleanup()

    def write(self,values=None,banner=None,init=None,release=None,enabled=True,
              early_release=False,extra='',end=None,release_before_end=False):
        fields = self.values if values is None else values
        identity = fields.get('request','one')
        summary = '[suffix] '+' '.join(f'{k}={v}' for k,v in fields.items())+'\n' if fields else ''
        start = self.init if init is None else init
        release_line = self.release if release is None else release
        request_end = self.end.replace('id=one',f'id={identity}') if end is None else end
        tail = (release_line+request_end if release_before_end else request_end+release_line)
        text = (start + (self.banner if banner is None else banner) +
                (release_line if early_release else '') + f'[q4t][request] id={identity} event=admitted stage=request\n' +
                summary + extra + (request_end if early_release else tail) +
                '[startup-reservation] {"event":"finalized","status":"PASS",'
                '"release_unknown":0}\n')
        (self.output/'server.log').write_text(text)
        (self.output/'server-command.json').write_text(json.dumps(
            {'argv':['q4t','serve']+(['--mtp-suffix'] if enabled else [])}))

    def test_records_short_gate_and_nested_timing(self):
        self.write()
        audit_server(self.output,1)
        record = json.loads((self.output/'admission-audit.json').read_text())
        self.assertEqual(record['min_admitted_match'],32)
        self.assertEqual(record['requests'][0]['short_rejected'],'1')
        self.assertIn('included',record['cost_scope'])

    def test_zero_queries_with_real_index_allocation_are_valid(self):
        values = dict(self.values)
        for key in ['queries','hits','no_match','incomplete','d0_mismatch',
                    'numeric_checks','classified_tokens','numeric_rejected','short_rejected','numeric_ns']:
            values[key] = 0
        self.write(values=values)
        audit_server(self.output,1)

    def test_old_numeric_only_and_wrong_rule_binaries_are_rejected(self):
        for banner in ['',self.banner.replace(' min_admitted_match=32',''),
                       self.banner.replace('=32','=31'),self.banner.replace('=32','=320'),
                       self.banner.replace('numeric_gate=1','numeric_gate=0')]:
            with self.subTest(banner=banner),self.assertRaises(RuntimeError):
                self.write(banner=banner)
                audit_server(self.output,1)

    def test_table_ownership_and_order_are_required(self):
        for kwargs in [dict(init=''),dict(release=''),dict(init=self.init*2),
                       dict(release=self.release*2),dict(early_release=True),
                       dict(init=self.init.replace('bytes=248320','bytes=65536')),
                       dict(init=self.init.replace('cap_bytes=262144','cap_bytes=999999')),
                       dict(init=self.init.replace('unknown=243','unknown=0'))]:
            with self.subTest(kwargs=kwargs),self.assertRaises(RuntimeError):
                self.write(**kwargs)
                audit_server(self.output,1)

    def test_outcome_accounting_and_nested_cost_must_close(self):
        for key,value in [('short_rejected',2),('short_rejected',-1),('numeric_checks',7),
                          ('no_match',2),('numeric_ns',1001),('peak_bytes',0)]:
            with self.subTest(key=key,value=value),self.assertRaises(RuntimeError):
                self.write(values=dict(self.values,**{key:value}))
                audit_server(self.output,1)
        with self.assertRaises(RuntimeError):
            self.write(values={k:v for k,v in self.values.items() if k!='short_rejected'})
            audit_server(self.output,1)

    def test_request_end_release_and_table_lifetime_must_close(self):
        for kwargs in [dict(end=''),dict(end=self.end*2),
                       dict(end=self.end.replace('released=1','released=0')),
                       dict(end=self.end.replace('id=one','id=other')),
                       dict(release_before_end=True)]:
            with self.subTest(kwargs=kwargs),self.assertRaises(RuntimeError):
                self.write(**kwargs)
                audit_server(self.output,1)

    def test_classified_token_bounds_distinguish_hits_from_short_rejections(self):
        values = dict(self.values,queries=1,hits=1,no_match=0,incomplete=0,d0_mismatch=0,
                      numeric_checks=1,numeric_rejected=0,short_rejected=0,classified_tokens=32)
        self.write(values=values)
        audit_server(self.output,1)
        with self.assertRaises(RuntimeError):
            self.write(values=dict(values,classified_tokens=31))
            audit_server(self.output,1)
        values.update(hits=0,short_rejected=1,classified_tokens=31)
        self.write(values=values)
        audit_server(self.output,1)
        with self.assertRaises(RuntimeError):
            self.write(values=dict(values,classified_tokens=32))
            audit_server(self.output,1)

    def test_explicit_cancel_requires_real_suffix_use_and_cancelled_end(self):
        values = dict(self.values,request='one-cancel')
        end = ('[q4t][request] id=one-cancel event=end released=1 '
               'outcome=cancelled reason=explicit_cancel\n')
        self.write(values=values,end=end)
        audit_server(self.output,1)
        # Preserve query/classification closure so only absent suffix use fails.
        with self.assertRaises(RuntimeError):
            self.write(values=dict(values,hits=0,short_rejected=4),end=end)
            audit_server(self.output,1)
        for invalid_end in [end.replace('outcome=cancelled','outcome=success'),
                            end.replace('reason=explicit_cancel','reason=completed')]:
            with self.subTest(end=invalid_end),self.assertRaises(RuntimeError):
                self.write(values=values,end=invalid_end)
                audit_server(self.output,1)

    def test_suffix_off_has_no_table_or_suffix_summary(self):
        self.write(values={},init='',release='',banner='',enabled=False)
        audit_server(self.output,1)
        with self.assertRaises(RuntimeError):
            self.write(values={},enabled=False)
            audit_server(self.output,1)

    def test_explicit_fallback_never_passes_with_healthy_summary(self):
        for marker in ['MTP trunk alloc failed','MTP load failed','plain decode for this request']:
            with self.subTest(marker=marker),self.assertRaises(RuntimeError):
                self.write(extra=marker+'\n')
                audit_server(self.output,1)


if __name__=='__main__':
    unittest.main()

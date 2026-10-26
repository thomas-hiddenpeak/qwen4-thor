"""Run only after the first real HTTP gate: reject corrupted causal evidence."""
import copy
from pathlib import Path
import unittest
from run_stage import audit_rows

ROOT=Path(__file__).resolve().parents[2]

class Controls(unittest.TestCase):
    def setUp(self):
        self.case=dict(mode='N1',input_tokens=10,expected_work=dict(verify=2,neural=4,restore=1,extend=2,extend_rows=7,tail=0))
        self.row=dict(request='r0',ordinal=0,mode='N1',input=10,arrive_ns=1,target_done_ns=2,prefill_done_ns=3,
                      selected_ns=4,first_round_ns=5,first_content_ns=6,generation_done_ns=7,
                      **self.case['expected_work'],ready=0,queries=0,hits=0,init_ns=0,query_ns=0,append_ns=0,release_ns=1,peak_bytes=0)
    def raw(self):
        return 'event=admitted id=r0 \nevent=end id=r0 released=1 \n[suffix-causal] '+ ' '.join(f'{k}={v}' for k,v in self.row.items())+'\n'
    def test_valid_neural_empty_reset(self):
        self.assertEqual(len(audit_rows(self.raw(),[self.case])),1)
    def test_valid_shadow_and_real(self):
        for mode in ['Q','R']:
            self.case['mode']=self.row['mode']=mode
            self.row.update(queries=2,init_ns=1,query_ns=2,append_ns=1,peak_bytes=100)
            self.assertEqual(len(audit_rows(self.raw(),[self.case])),1)
    def test_contract_mutations(self):
        for key,value in [('ordinal',1),('mode','Q'),('input',11),('ready',1),('hits',1),('verify',3),
                          ('neural',3),('restore',0),('extend',1),('extend_rows',6),('tail',1),
                          ('queries',1),('init_ns',1),('query_ns',1),('append_ns',1),('peak_bytes',1),
                          ('prefill_done_ns',8),('first_content_ns',0),('release_ns',-1)]:
            before=copy.deepcopy(self.row);self.row[key]=value
            with self.subTest(key=key),self.assertRaises(RuntimeError): audit_rows(self.raw(),[self.case])
            self.row=before
    def test_missing_or_duplicate_lifecycle(self):
        for raw in [self.raw().replace('released=1','released=0'),self.raw()+'event=admitted id=r0 \n',
                    self.raw().replace('event=end id=r0 released=1','')]:
            with self.assertRaises(RuntimeError): audit_rows(raw,[self.case])
    def test_fallback(self):
        with self.assertRaises(RuntimeError): audit_rows(self.raw()+'[suffix] disabled=x\n',[self.case])
    def test_record_budget(self):
        with self.assertRaises(RuntimeError): audit_rows(self.raw().rstrip()+' x='+'a'*2048+'\n',[self.case])
    def test_query_required(self):
        self.case['mode']=self.row['mode']='Q'
        with self.assertRaises(RuntimeError): audit_rows(self.raw(),[self.case])
    def test_source_selection_after_prefill(self):
        source=(ROOT/'src/server/chat_generation.cpp').read_text()
        select=source.index('suffix_causal_.AfterPrefill(')
        self.assertGreater(select,source.index('d_trunk_full = nullptr;',source.index('void ChatServer::HandleChat')))
        self.assertIn('causal.prefill_done_ns = SuffixCausalRequest::Now();',source[:select])
        self.assertNotIn('causal.choice.',source[:select])
        self.assertIn('proposal.ready && apply_suffix',source)
        self.assertIn('if (use_mtp && query_suffix)',source)
        self.assertIn('if (causal.enabled) suffix.Reset();',source)

if __name__=='__main__': unittest.main(verbosity=2)

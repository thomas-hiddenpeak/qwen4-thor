import unittest
from parse_reports import EXPECTED,parse

def fixture():
 lines=[]
 for t in [8192]*24+[8181]:
  for family,fields in EXPECTED.items():
   values={k:1.0 for k in fields};values['total']=6.0 if family=='sub-timing' else float(len(fields)-1)
   lines.append(f'[{family}] T={t} '+' '.join(f'{k}={v:.2f}' for k,v in values.items()))
 return '\n'.join(lines)
class CoverageTests(unittest.TestCase):
 def test_prefill_needs_top_moe_not_decode_detail(self):
  r=parse(fixture());self.assertEqual(len(r['sub-timing']),25);self.assertEqual(r['sub-timing'][0]['moe'],1)
 def test_decode_detail_not_prefill(self):
  self.assertEqual(parse(fixture()+'\n[moe-subtiming] T=1 ignored=1'),parse(fixture()))
 def test_incomplete_shape_and_missing_family(self):
  for text in [fixture().replace('T=8181','T=8192'), '\n'.join(l for l in fixture().splitlines() if not l.startswith('[hc-mix-subtiming]'))]:
   with self.assertRaises(ValueError):parse(text)
 def test_missing_field_and_bad_sum_and_sync(self):
  for text in [fixture().replace('full_core=1.00','full_core=nan',1),fixture().replace('moe=1.00','moe=3.00',1),fixture()+'\n[sub-timing] sync failed: test']:
   with self.assertRaises(ValueError):parse(text)
 def test_unexpected_prefill_decode_detail_rejected(self):
  with self.assertRaises(ValueError):parse(fixture()+'\n[moe-subtiming] T=8192 prep_gu=1')
if __name__=='__main__':unittest.main()

"""Prefill coverage: top-level MoE exists; MoE-decode detail does not."""
import math,re
EXPECTED={'layer-timing':['embed','lin','full','head','total'],
'sub-timing':['ple','grread','attn','wrar','moe','write','total','lin_inproj','lin_state','lin_outproj','full_qkv','full_core','full_oproj'],
'hc-mix-subtiming':['norm','down_gemm','silu','up_gemm','gate','inject','combine','total']}
def parse(text):
 if 'sync failed:' in text:raise ValueError('timing sync failure')
 families={key:[] for key in EXPECTED}
 for line in text.splitlines():
  m=re.match(r'\[([^]]+)\] T=(\d+) ',line)
  if not m or int(m[2])<=1:continue
  if m[1]=='moe-subtiming':raise ValueError('unexpected prefill MoE-decode detail; re-audit coverage')
  if m[1] not in families:continue
  fields={k:float(x) for k,x in re.findall(r'([a-z_]+)=([\d.]+)',line)}
  if set(fields)!=set(EXPECTED[m[1]]):raise ValueError('missing or unexpected fields')
  if not all(math.isfinite(x) and x>=0 for x in fields.values()):raise ValueError('invalid value')
  if m[1]=='sub-timing' and abs(sum(fields[k] for k in ['ple','grread','attn','wrar','moe','write'])-fields['total'])>.1:raise ValueError('top level sum mismatch')
  families[m[1]].append({'T':int(m[2]),**fields})
 for key,rows in families.items():
  if [r['T'] for r in rows]!=[8192]*24+[8181]:raise ValueError(f'wrong coverage: {key}, {len(rows)} reports')
 return families
if __name__=='__main__':
 import sys
 from pathlib import Path
 parse(Path(sys.argv[1]).read_text());print('prefill coverage passed (three supported families)')

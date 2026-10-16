import re
APIS={'cudaMalloc','cudaMallocAsync','cudaMallocHost','cudaHostAlloc','cudaFree','cudaFreeAsync','cudaFreeHost'}
def parse(text):
 rows=[]
 for line in text.splitlines():
  if '[allocation-address]' not in line:continue
  assert line.startswith('[allocation-address] '),line
  r=dict(re.findall(r'(\w+)=([^ ]+)',line))
  assert set(r)=={'id','tid','api','begin_ns','end_ns','bytes','ptr','stream','flags','rc'},r
  assert r['api'] in APIS
  for k in ['id','tid','begin_ns','end_ns','bytes','flags','rc']:r[k]=int(r[k])
  for k in ['ptr','stream']:r[k]=0 if r[k]=='(nil)' else int(r[k],16)
  assert r['end_ns']>=r['begin_ns'] and r['bytes']>=0
  rows.append(r)
 rows.sort(key=lambda r:r['id'])
 assert rows and [r['id'] for r in rows]==list(range(len(rows)))
 # cudaMallocHost is router tracing only, disabled in this request.
 assert APIS-{'cudaMallocHost'} <= {r['api'] for r in rows}
 return rows

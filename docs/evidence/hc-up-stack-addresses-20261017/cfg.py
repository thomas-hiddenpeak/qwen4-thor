import re,collections
def constants(row):
 seq=row['instructions'];states={0:{}};queue=collections.deque([0])
 def key(reg):return 'x'+reg[1:] if re.fullmatch(r'[xw]\d+',reg) else reg
 while queue:
  i=queue.popleft();state=states[i];after=state.copy()
  text=seq[i].split('//',1)[0].strip();op=text.split()[0]
  operands=text.split(None,1)[1] if len(text.split(None,1))==2 else ''
  first=operands.split(',')[0].strip()
  if op in ('bl','blr'):
   for j in range(19):after.pop('x'+str(j),None)
  elif op not in ('b','br','ret','cmp','cmn','ccmp','tst','cbz','cbnz','tbz','tbnz','str','strb','strh','stur','stp','nop') and not op.startswith('b.'):
   after.pop(key(first),None)
   if op=='ldp':after.pop(key(operands.split(',')[1].strip()),None)
  if m:=re.match(r'adrp\s+(x\d+), ([0-9a-f]+)',text):after[m[1]]=(int(m[2],16),frozenset([i]))
  elif m:=re.match(r'add\s+(x\d+), (x\d+), #0x([0-9a-f]+)',text):
   if m[2] in state:
    value,origins=state[m[2]];after[m[1]]=(value+int(m[3],16),origins)
  elif m:=re.match(r'mov\s+(x\d+), (x\d+)$',text):
   if m[2] in state:after[m[1]]=state[m[2]]
  # Any GP base written back by a load/store becomes unknown.
  for m in re.finditer(r'\[(x\d+)[^\]]*\](?:!|,)',text):after.pop(m[1],None)
  # Conservative eight-byte stack slots. Calls and unknown stores kill all.
  stack_keys=[k for k in after if k.startswith('@')]
  load=re.fullmatch(r'ldr\s+(x\d+), \[sp(?:, #(\d+))?\]',text)
  store=re.fullmatch(r'str\s+(x\d+|xzr), \[sp(?:, #(\d+))?\]',text)
  span=re.fullmatch(r'(str|strb|strh)\s+([xwqdsb])(?:\d+|zr), \[sp(?:, #(\d+))?\]',text)
  writes_memory=op.startswith(('st','cas','swp','ldadd','ldclr','ldeor','ldset'))
  changes_sp=first=='sp' or bool(re.search(r'\[sp[^\]]*\](?:!|,)',text))
  if op in ('bl','blr') or changes_sp or (writes_memory and not span):
   for k in stack_keys:after.pop(k,None)
  if span:
   offset=int(span[3] or 0)
   width={'x':8,'w':4,'q':16,'d':8,'s':4,'b':1}[span[2]]
   if span[1]=='strb':width=1
   if span[1]=='strh':width=2
   for k in stack_keys:
    if max(offset,int(k[1:]))<min(offset+width,int(k[1:])+8):after.pop(k,None)
   if store and store[1] in state:after['@'+str(offset)]=state[store[1]]
  if load and not changes_sp:
   k='@'+str(int(load[2] or 0))
   if k in state:after[load[1]]=state[k]
  targets=[]
  branch=op=='b' or op.startswith('b.') or op in ('cbz','cbnz','tbz','tbnz')
  if branch:
   m=re.search(r'([0-9a-f]+) <[^>]+>',text);assert m
   address=int(m[1],16)
   if row['address']<=address<row['address']+row['size']:targets.append((address-row['address'])//4)
  if op not in ('b','br','ret') and i+1<len(seq):targets.append(i+1)
  for j in targets:
   if j not in states:merged=after.copy()
   else:
    merged={k:(v[0],v[1]|after[k][1]) for k,v in states[j].items() if k in after and v[0]==after[k][0]}
   if j not in states or merged!=states[j]:states[j]=merged;queue.append(j)
 return states

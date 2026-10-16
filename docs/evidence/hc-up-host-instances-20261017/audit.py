"""Keep every nm function instance; compare mnemonic multisets per name.

This is a coverage repair, not operand-equivalence or reachability proof.
"""
import collections,hashlib,json,re,subprocess
from pathlib import Path
ROOT=Path('/home/rm01/models/dev/qwen4-thor');OUT=Path(__file__).resolve().parent
sources={'A':ROOT/'.q4t-work/mtp-decode-20261016/q4t-main','B':ROOT/'.q4t-work/mtp-decode-20261016/hc-up/q4t'}
expected={'A':'be74946d815904e84899c64fd88511cb0474166bef9d199c22330e8413db5072','B':'f01a0ee63f626c352f66677952368d43db02c9231576a2b8ba1ca33a915ce573'}
records={};groups={};coverage={}
for v,binary in sources.items():
 assert hashlib.sha256(binary.read_bytes()).hexdigest()==expected[v]
 nm=subprocess.check_output(['nm','-S','--defined-only',str(binary)],text=True)
 asm=subprocess.check_output(['objdump','-d','--no-show-raw-insn',str(binary)],text=True)
 (OUT/f'{v}-symbols.txt').write_text(nm);(OUT/f'{v}-host.asm').write_text(asm)
 instructions={int(m[1],16):m[2] for line in asm.splitlines() if (m:=re.match(r'^\s+([0-9a-f]+):\s+(.*)',line))}
 rows=[];by_name=collections.defaultdict(list);covered=set()
 for line in nm.splitlines():
  fields=line.split()
  if len(fields)!=4 or fields[2] not in 'tTwW':continue
  address,size=int(fields[0],16),int(fields[1],16)
  assert address%4==0 and size%4==0
  seq=[instructions.get(i,'<missing>') for i in range(address,address+size,4)]
  assert '<missing>' not in seq,(v,line)
  mnemonics=[s.split()[0] for s in seq]
  item={'instance':len(rows),'name':fields[3],'address':address,'size':size,'binding':fields[2],
        'mnemonic_sha256':hashlib.sha256('\n'.join(mnemonics).encode()).hexdigest(),
        'instructions':seq}
  rows.append(item);by_name[item['name']].append(item);covered.update(range(address,address+size,4))
 records[v]=rows;groups[v]=by_name
 coverage[v]={'function_rows':len(rows),'distinct_names':len(by_name),'duplicate_names':{n:len(g) for n,g in by_name.items() if len(g)>1},'covered_instruction_addresses':len(covered),'all_disassembled_addresses':len(instructions),'uncovered_disassembly_addresses':len(set(instructions)-covered)}
 (OUT/f'{v}-instances.json').write_text(json.dumps(rows,indent=2)+'\n')
changes={};matched=0
for name in sorted(set(groups['A'])|set(groups['B'])):
 ag=groups['A'].get(name,[]);bg=groups['B'].get(name,[])
 ca=collections.Counter(r['mnemonic_sha256'] for r in ag);cb=collections.Counter(r['mnemonic_sha256'] for r in bg)
 matched+=sum((ca&cb).values())
 if ca==cb:continue
 def residual(rows,counts):
  result=[];counts=counts.copy()
  for row in rows:
   sha=row['mnemonic_sha256']
   if counts[sha]>0:
    result.append({k:v for k,v in row.items() if k!='instructions'});counts[sha]-=1
  return result
 changes[name]={'A_count':len(ag),'B_count':len(bg),'A_unmatched':residual(ag,ca-cb),'B_unmatched':residual(bg,cb-ca),'pairing_limit':'no guessed pairing among ambiguous duplicate instances'}
old=json.loads((ROOT/'.q4t-work/hc-up-cause-20261016/host-opcode-review.json').read_text())
result={'sha256':expected,'coverage':coverage,'mnemonic_multiset_matched_instances':matched,'changed_groups':changes,'newly_exposed_groups':sorted(set(changes)-set(old['opcode_sequence_changed'])),'limits':['Mnemonic agreement ignores operands and addresses; not proof of semantic/CPU cost equality','Duplicate groups compared as multisets, without assuming ordinal pairing across added functions','Uncovered addresses include PLT, padding or zero-size symbols; not whole executable coverage','No static name-based claim of off reachability'], 'new_gpu_requests':0}
(OUT/'result.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({'coverage':coverage,'matched':matched,'changed_groups':[(n,v['A_count'],v['B_count']) for n,v in changes.items()],'newly_exposed_groups':result['newly_exposed_groups']},indent=2))

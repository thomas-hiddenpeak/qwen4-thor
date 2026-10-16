import subprocess,re,json,hashlib
from pathlib import Path
r=Path(__file__).resolve().parent
bins={'A':'.q4t-work/mtp-decode-20261016/q4t-main','B':'.q4t-work/mtp-decode-20261016/hc-up/q4t'}
funcs={}
for side,p in bins.items():
 text=subprocess.check_output(['objdump','-d','--no-show-raw-insn',p],text=True); (r/(side+'-host.asm')).write_text(text)
 symbols={}
 for line in (r/(side+'-symbols.txt')).read_text().splitlines():
  v=line.split()
  if len(v)==4 and v[2] in 'tTwW':symbols[v[3]]=(int(v[0],16),int(v[1],16))
 instructions={int(m[1],16):m[2] for line in text.splitlines() if (m:=re.match(r'^\s+([0-9a-f]+):\s+(\S+)',line))}
 d={n:[instructions.get(i,'<missing>') for i in range(a,a+size,4)] for n,(a,size) in symbols.items()}
 funcs[side]=d
common=funcs['A'].keys()&funcs['B'].keys();changed={n:[len(funcs['A'][n]),len(funcs['B'][n])] for n in common if funcs['A'][n]!=funcs['B'][n]}
v={'common_disassembled_symbols':len(common),'opcode_sequence_changed':changed,'boundary':'Mnemonic comparison only, not equality of operands or runtime addresses.'}
(r/'host-opcode-review.json').write_text(json.dumps(v,indent=2)+'\n');print(json.dumps(v,indent=2)[:4000])

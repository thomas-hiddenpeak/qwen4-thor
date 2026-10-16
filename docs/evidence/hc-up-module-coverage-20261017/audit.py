"""Read-only audit of registration intervention and trace module coverage."""
import hashlib,importlib.util,json,sqlite3,struct
from pathlib import Path
root=Path('/home/rm01/models/dev/qwen4-thor');out=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('elf',root/'docs/evidence/hc-up-thunk-cause-20261016/prepare.py')
elf=importlib.util.module_from_spec(spec);spec.loader.exec_module(elf)
stage=root/'.q4t-work/hc-up-registration-cause-20261016'
identity=json.loads((stage/'derivation.json').read_text())
data={v:(stage/v/'q4t').read_bytes() for v in ('A','B','C')}
for v,b in data.items():assert hashlib.sha256(b).hexdigest()==identity['variants'][v]['sha256']
sections=elf.sections(data['B']);text=sections['.text'];offset=text[4]+0x9020-text[3]
changed=[i for i,(b,c) in enumerate(zip(data['B'],data['C'])) if b!=c]
assert len(data['B'])==len(data['C']) and changed
assert all(offset<=i<offset+4 for i in changed)
assert struct.unpack_from('<I',data['C'],offset)[0]==0xd65f03c0
same_sections=[]
for name,s in sections.items():
 if name!='.text' and s[1]!=8:
  assert data['B'][s[4]:s[4]+s[5]]==data['C'][s[4]:s[4]+s[5]]
  same_sections.append(name)
traces={}
for v in ('A','B'):
 p=root/f'.q4t-work/hc-up-clean-trace-v2-20261017/{v}/context-204800/trace.sqlite'
 with sqlite3.connect('file:'+str(p)+'?mode=ro',uri=True) as c:
  tables=[r[0] for r in c.execute("select name from sqlite_master where type='table'")]
  columns=[r[1] for r in c.execute('pragma table_info(CUPTI_ACTIVITY_KIND_KERNEL)')]
  traces[v]={'cupti_tables':[t for t in tables if t.startswith('CUPTI')],
             'kernel_columns':columns,
             'module_tables':[t for t in tables if 'MODULE' in t.upper() or 'FUNCTION' in t.upper()]}
  assert not traces[v]['module_tables']
  assert not any('address' in x.lower() or 'module' in x.lower() for x in columns)
header=root/Path('/usr/local/cuda-13.3/targets/sbsa-linux/include/cupti_callbacks.h')
result={'binary_sha256':{v:hashlib.sha256(b).hexdigest() for v,b in data.items()},
 'B_to_C_changed_file_offsets':changed,'constructor_entry_va':'0x9020','instruction_C':'RET',
 'unchanged_non_text_sections':same_sections,
 'intervention':'Only skip HC registration constructor; embedded device payload remains byte-identical in B/C, no relink. Runtime registration/module state changes, host layout unchanged.',
 'old_guard_status':json.loads((stage/'lease/exit.json').read_text())['status'],
 'old_environment':json.loads((stage/'environment.json').read_text()),'clean_trace_coverage':traces,
 'limits':['Static fatbin preservation does not imply runtime loaded GPU code address preservation.',
           'Neither old registration stage nor post-ready clean traces recorded startup module loading.',
           'CUDA_MODULE_LOADING was not explicitly set in old recorded environment; do not infer actual loading mode from its absence.',
           'CUPTI module callback provides moduleId, cubinSize, pCubin (host image pointer); not a GPU instruction address.',
           'Old failed numerical gate and ENV_ABORT remain unchanged; no rerun authorized by this audit.'],
 'local_header_sha256':hashlib.sha256(header.read_bytes()).hexdigest(),
 'next_discriminating_observation':'Record startup MODULE_LOADED callbacks with cubin hashes to determine whether unused HC code is actually loaded; this can test loading occurrence but cannot directly prove instruction placement or TTFT cause.',
 'new_gpu_requests':0,'root_cause_proven':False}
(out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({k:result[k] for k in ('B_to_C_changed_file_offsets','old_guard_status','old_environment','next_discriminating_observation')},indent=2))

import ast,hashlib,json,typing,unicodedata,time
from pathlib import Path
import numpy as np
start=time.monotonic();a=Path('.q4t-work/serve-audio-aligner-20261117');source=a/'metadata-01/qwen3_forced_aligner.py';parsed=ast.parse(source.read_text());cls=next(x for x in parsed.body if isinstance(x,ast.ClassDef) and x.name=='Qwen3ForceAlignProcessor');namespace={'List':typing.List,'unicodedata':unicodedata};exec(compile(ast.Module(body=[cls],type_ignores=[]),str(source),'exec'),namespace);proc=namespace['Qwen3ForceAlignProcessor'].__new__(namespace['Qwen3ForceAlignProcessor'])
texts=['Hello, world!', '你好，世界！', "don't well-being", ' e\u0301 café ', 'abc中def国ghi', '<timestamp><|audio_pad|>', '𠀀漢字123', '٣٤٥ Ⅳ ²', '中文\u00a0English\x1c中文', '...👩🏽\u200d💻?!', 'Привет мир', 'français déjà', "l'été à Paris"]
words=[{'text':t,'words':proc.tokenize_space_lang(t)} for t in texts]
cases=[]
for ids in [[],[1,2],[5,4],[4,3,2,1],[1,3,2,4,4,5],[1,9,8,7,6,5,2,3,4,10],[9,8,1,2,3,4],[1,2,3,4,9,8],[1,1,1,1]]:
 raw=np.asarray(ids,dtype=np.float64)*80;fixed=proc.fix_timestamp(raw) if ids else []
 cases.append({'classes':ids,'expected_ms':fixed})
real=[]
for i in range(2):
 ids=np.fromfile(a/f'compute-gpu-01/run/native/case-{i}/classes.u32','<u4');raw=ids.astype(np.float64)*80;real.append({'classes':ids.tolist(),'expected_ms':proc.fix_timestamp(raw)})
result={'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'status':'CPU_REFERENCE_ONLY','seconds':time.monotonic()-start,'words':words,'timestamps':cases,'actual':real}
Path('tests/fixtures/aligner/postprocess-golden.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');print(json.dumps({k:v for k,v in result.items() if k not in ('words','timestamps','actual')}))

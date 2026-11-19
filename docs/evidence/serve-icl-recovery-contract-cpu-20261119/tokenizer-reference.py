import hashlib, json, time
from pathlib import Path
import transformers, tokenizers
from transformers import Qwen2Tokenizer
start=time.monotonic()
assets=Path('.q4t-work/serve-custom-voices-20261118/base-service-assets-01')
tok=Qwen2Tokenizer.from_pretrained(str(assets),local_files_only=True)
texts={'reference':'The meeting starts tomorrow morning. Please bring your notebook.','speech':'The blue train arrives at noon.','chat':'Please close the window.'}
ids={k:tok.encode(v,add_special_tokens=False) for k,v in texts.items()}
assert all(tok.decode(ids[k])==v for k,v in texts.items())
result=dict(status='PASS_FIXED_TOKENIZER_ROUNDTRIP',texts=texts,ids=ids,input_tokens={k:len(v)+len(ids['reference'])+6+56*16 for k,v in ids.items() if k!='reference'},assets={n:hashlib.sha256((assets/n).read_bytes()).hexdigest() for n in ('vocab.json','merges.txt','tokenizer_config.json')},versions=dict(transformers=transformers.__version__,tokenizers=tokenizers.__version__),seconds=time.monotonic()-start,gpu_actions=0)
Path('.q4t-work/serve-tts-icl-20261119/recovery-contract-cpu-01/token-reference.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(result,ensure_ascii=False))

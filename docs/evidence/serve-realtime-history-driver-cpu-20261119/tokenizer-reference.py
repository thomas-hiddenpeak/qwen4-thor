from pathlib import Path
import json,hashlib,transformers,tokenizers
from transformers import Qwen2Tokenizer
root=Path.cwd();assets=root/'.q4t-work/serve-audio-native-20261117/assets-01'
source=root/'.q4t-work/serve-realtime-20261119/transcription-business-04/run/alpha.sdk.json'
first=json.loads(source.read_text())['turns'][0]['terminal']
text='\nPrevious transcription:\n'+first['transcript']
tok=Qwen2Tokenizer.from_pretrained(str(assets),local_files_only=True)
ids=tok.encode(text,add_special_tokens=False)
assert ids and tok.decode(ids)==text and len(tok.get_vocab())>150000
sha=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest()
result=dict(prior_transcript=first['transcript'],context=text,base_input_tokens=first['usage']['input_tokens'],history_tokens=len(ids),token_ids=ids,versions=dict(transformers=transformers.__version__,tokenizers=tokenizers.__version__),files={str(p):sha(p) for p in [source,*[assets/name for name in ['vocab.json','merges.txt','tokenizer_config.json']]]})
print(json.dumps(result,ensure_ascii=False))

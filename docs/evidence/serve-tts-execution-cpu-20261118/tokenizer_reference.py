import hashlib
import inspect
import json
import os
from pathlib import Path
import sys
import time

assert os.environ.get('USE_TORCH') == '0'
assert os.environ.get('CUDA_VISIBLE_DEVICES') == ''
import transformers
import tokenizers
from transformers import Qwen2TokenizerFast

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / 'assets-1b7-01'
start = time.monotonic()
tokenizer = Qwen2TokenizerFast.from_pretrained(ASSETS, local_files_only=True)
assert tokenizer.vocab_size == 151643
assert len(tokenizer) == 151676
backend = json.loads(tokenizer.backend_tokenizer.to_str())
assert backend['normalizer'] == {'type': 'NFC'}
post = backend['post_processor']
assert post is None or (post['type'] == 'TemplateProcessing' and
                        post['single'] == [{'Sequence': {'id': 'A', 'type_id': 0}}]
                        and post['special_tokens'] == {})
assert len(backend['model']['merges']) == 151387
assert len(backend['added_tokens']) == 33
torch_module = sys.modules.get('torch')
assert torch_module is None or not torch_module.cuda.is_initialized()
texts = [
    '', 'Hello', 'Hello, world!', '你好，世界！',
    '<|im_start|>assistant\nHello, world!<|im_end|>\n<|im_start|>assistant\n',
    '<|im_start|>user\nSpeak cheerfully.<|im_end|>\n',
    '今天下午三点开会。', 'The meeting starts at three.', 'e\u0301 café',
    '👩🏽‍💻 says hello.', 'a  b\tc\n',
    '<tts_pad><tts_text_bos><tts_text_eod><|audio_pad|>',
    '<asr_text><non_speech>', '<blank1><blank27>', '12345 ٣٤٥',
    'العربية 한국어 日本語',
]
cases = []
for text in texts:
    ids = tokenizer.encode(text, add_special_tokens=False)
    assert ids == tokenizer.encode(text, add_special_tokens=True)
    cases.append({'text': text, 'ids': ids,
                  'decoded': tokenizer.decode(ids, skip_special_tokens=False),
                  'skip_decoded': tokenizer.decode(ids, skip_special_tokens=True)})
source = Path(inspect.getfile(Qwen2TokenizerFast))
result = {'status': 'PASS_CPU_REFERENCE_GENERATION_ONLY',
          'transformers': transformers.__version__,
          'tokenizers': tokenizers.__version__, 'source': str(source),
          'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
          'asset_revision': '0c0e3051f131929182e2c023b9537f8b1c68adfe',
          'model_actions': 0, 'cuda_initialized': False, 'torch_imported': torch_module is not None, 'seconds': time.monotonic() - start,
          'assets': {name: hashlib.sha256((ASSETS / name).read_bytes()).hexdigest()
                     for name in ('vocab.json', 'merges.txt', 'tokenizer_config.json')},
          'cases': cases}
(ROOT / 'tokenizer-reference-01.json').write_text(
    json.dumps(result, ensure_ascii=False, indent=2) + '\n')
(ROOT / 'tokenizer-reference-backend.json').write_text(
    json.dumps({key: value for key, value in backend.items() if key != 'model'},
               ensure_ascii=False, indent=2) + '\n')
print(json.dumps({k: v for k, v in result.items() if k not in ('cases', 'assets')},
                 ensure_ascii=False))

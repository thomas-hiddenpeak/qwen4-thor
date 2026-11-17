#!/usr/bin/env python3
"""Pinned official ASR CPU reference under the stage's strict shared lease.

Adds the encoder's explicit cu_seqlens block mask to the official eager path;
this documented adaptation restores its intended FA2 window visibility. It
does not claim bitwise equivalence with FlashAttention or qualify audio codecs.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import struct
import sys
import time
import types


SOURCES = {
    'configuration_qwen3_asr':
        'acf6c3f1cb3dc1ea0df621a11f7df6ccca109be73b6bf51508b5052942f5bec1',
    'modeling_qwen3_asr':
        '2fb5d98da1933748f5117ee05ce4e7150c9ead8154fb8e25f7af3968b853adc7',
}


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as file:
        while block := file.read(1 << 20):
            value.update(block)
    return value.hexdigest()


def import_official(directory):
    package = types.ModuleType('q4t_asr_reference')
    package.__path__ = [str(directory)]
    sys.modules[package.__name__] = package
    for name, expected in SOURCES.items():
        source = directory / (name + '.py')
        assert digest(source) == expected
        spec = importlib.util.spec_from_file_location(
            package.__name__ + '.' + name, source)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return sys.modules['q4t_asr_reference.modeling_qwen3_asr']


def stored_heads(directory):
    result = []
    with (directory / 'model.safetensors').open('rb') as file:
        size = struct.unpack('<Q', file.read(8))[0]
        assert size <= 1024 * 1024
        header = json.loads(file.read(size))
        for name in ('thinker.model.embed_tokens.weight', 'thinker.lm_head.weight'):
            start, end = header[name]['data_offsets']
            assert end - start == 311164928
            file.seek(size + 8 + start)
            left = end - start
            value = hashlib.sha256()
            while left:
                block = file.read(min(left, 1 << 20))
                assert block
                value.update(block)
                left -= len(block)
            result.append({'name': name, 'offset': start, 'sha256': value.hexdigest()})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['assets', 'official-source', 'plan', 'native', 'output']:
        parser.add_argument('--' + name, required=True, type=Path)
    args = parser.parse_args()
    os.fstat(int(os.environ['Q4T_MANAGED_LEASE_FD']))
    os.environ['CUDA_VISIBLE_DEVICES'] = ''
    import numpy as np
    import torch
    import transformers
    from transformers import Qwen2TokenizerFast
    assert transformers.__version__ == '4.57.6'
    assert not torch.cuda.is_initialized()
    torch.set_num_threads(8)
    torch.set_num_interop_threads(1)
    started = time.monotonic()

    def checkpoint():
        assert time.monotonic() - started < 600, 'ASR reference time budget'

    module = import_official(args.official_source)
    original = module.Qwen3ASRAudioEncoderLayer.forward

    def masked_forward(self, hidden_states, cu_seqlens,
                       attention_mask=None, **kwargs):
        assert attention_mask is None
        length = hidden_states.shape[0]
        mask = torch.full((1, 1, length, length),
                          torch.finfo(hidden_states.dtype).min,
                          dtype=hidden_states.dtype, device=hidden_states.device)
        cuts = cu_seqlens.tolist()
        assert cuts[0] == 0 and cuts[-1] == length
        for first, end in zip(cuts, cuts[1:]):
            assert 0 < end - first <= 104
            mask[:, :, first:end, first:end] = 0
        return original(self, hidden_states, cu_seqlens,
                        attention_mask=mask, **kwargs)

    module.Qwen3ASRAudioEncoderLayer.forward = masked_forward
    assert digest(args.assets / 'model.safetensors') == (
        '79d6cbd4c98c7bbffe9db2edac07f56cd6637d0d5944b27f6c2b8353840323ea')
    heads = stored_heads(args.assets)
    model, loading = module.Qwen3ASRForConditionalGeneration.from_pretrained(
        args.assets, dtype=torch.bfloat16, attn_implementation='eager',
        local_files_only=True, trust_remote_code=False, output_loading_info=True)
    assert not any(loading.get(key) for key in (
        'missing_keys', 'unexpected_keys', 'mismatched_keys', 'error_msgs'))
    model.eval()
    thinker = model.thinker
    tied = thinker.lm_head.weight.data_ptr() == (
        thinker.model.embed_tokens.weight.data_ptr())
    # Separate stored entries do not imply different values. The pinned
    # checkpoint has byte-identical tables; official tying is then valid.
    assert not tied or heads[0]['sha256'] == heads[1]['sha256']
    assert next(model.parameters()).device.type == 'cpu'
    assert thinker.audio_tower.config._attn_implementation == 'eager'
    assert thinker.model.config._attn_implementation == 'eager'
    tokenizer = Qwen2TokenizerFast.from_pretrained(args.assets,
                                                   local_files_only=True)
    plan = json.loads(args.plan.read_text())
    assert 0 < len(plan['cases']) <= 3
    args.output.mkdir(parents=True, exist_ok=False)
    records = []
    forwards = 0
    for index, case in enumerate(plan['cases']):
        checkpoint()
        output = args.output / f'case-{index}'
        output.mkdir()

        def save(value, name):
            assert value.dtype == torch.bfloat16 and torch.isfinite(value).all()
            value.detach().contiguous().view(torch.uint16).numpy().tofile(output / name)

        assert digest(Path(case['mel'])) == case['mel_sha256']
        mel = torch.from_numpy(np.fromfile(case['mel'], '<f4').reshape(
            128, case['frames'])).bfloat16()
        hook = thinker.audio_tower.layers[0].register_forward_pre_hook(
            lambda layer, inputs: save(inputs[0], 'encoder-input.bf16'))
        try:
            with torch.inference_mode():
                audio = thinker.audio_tower(mel, feature_lens=torch.tensor(
                    [case['frames']], dtype=torch.long)).last_hidden_state
        finally:
            hook.remove()
        assert audio.shape == (case['audio_tokens'], 1024)
        save(audio, 'encoder.bf16')

        def embeddings(ids):
            result = thinker.model.embed_tokens(torch.tensor([ids]))
            start = case['audio_start']
            result[:, start:start + audio.shape[0]] = audio
            return result

        native_ids = np.fromfile(args.native / f'case-{index}' /
                                 'generated.u32', '<u4').tolist()
        assert 0 < len(native_ids) <= 128
        with torch.inference_mode():
            # Compare identical histories, even if free generation diverges.
            for step in range(min(4, len(native_ids))):
                checkpoint()
                ids = case['tokens'] + native_ids[:step]
                hidden = thinker.model(inputs_embeds=embeddings(ids),
                    attention_mask=torch.ones((1, len(ids)), dtype=torch.long),
                    use_cache=False).last_hidden_state
                logits = thinker.lm_head(hidden[:, -1])
                save(logits[0], f'logits-{step}.bf16')
                forwards += 1
            ids = case['tokens']
            result = thinker.model(inputs_embeds=embeddings(ids),
                attention_mask=torch.ones((1, len(ids)), dtype=torch.long),
                use_cache=True)
            generated = []
            for step in range(case['max_new_tokens']):
                checkpoint()
                logits = thinker.lm_head(result.last_hidden_state[:, -1])
                token = int(logits[0].float().argmax())
                generated.append(token)
                forwards += 1
                assert forwards <= 264
                if token in (151643, 151645):
                    break
                if step + 1 < case['max_new_tokens']:
                    result = thinker.model(input_ids=torch.tensor([[token]]),
                        attention_mask=torch.ones((1, len(ids) + step + 1),
                                                  dtype=torch.long),
                        past_key_values=result.past_key_values, use_cache=True)
        np.asarray(generated, dtype='<u4').tofile(output / 'generated.u32')
        text = tokenizer.decode(generated, skip_special_tokens=True,
                                clean_up_tokenization_spaces=False)
        (output / 'text.txt').write_text(text)
        record = {'index': index, 'generated_tokens': len(generated),
                  'eos': generated[-1] in (151643, 151645)}
        (output / 'result.json').write_text(json.dumps(record, indent=2) + '\n')
        records.append(record)
        print(json.dumps(record), flush=True)
    assert not torch.cuda.is_initialized()
    result = {'status': 'REFERENCE_COMPLETED_NOT_YET_COMPARED',
              'torch': torch.__version__, 'transformers': transformers.__version__,
              'device': 'cpu', 'dtype': 'bfloat16', 'sources': SOURCES,
              'checkpoint_loading': loading,
              'stored_heads': heads, 'reference_ties_heads': tied,
              'attention': 'official eager plus explicit cu_seqlens block mask',
              'decoder_forwards': forwards, 'cases': records,
              'seconds': time.monotonic() - started}
    (args.output / 'result.json').write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    main()

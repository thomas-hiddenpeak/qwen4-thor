#!/usr/bin/env python3
"""Pinned official talker/predictor reference, CPU only under the shared lease.

The unused whole-model codec wrapper is an explicit raising import sentinel;
no codec is constructed or certified here. The two official math modules are
loaded unmodified and SHA-bound. The unused voice-clone mel helper also
has a raising import sentinel; no acoustic preprocessing is certified here.
Prompt construction executes the official
whole-model generate preparation up to its talker.generate call.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
import types
from unittest import mock

SOURCES = {'configuration_qwen3_tts': 'f52867f14fde06a416dd14864d503ce6d13d0a08d5f5da30191e1c80c13f5d18', 'modeling_qwen3_tts': '25c42656bcf810f06ef6bc1839bd7083f3c8cfedac3a147c4060b4262b1c96a0'}


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1 << 20):
            value.update(chunk)
    return value.hexdigest()


def import_official(directory):
    # CustomVoice uses fixed codec speaker IDs, never a voice-clone waveform.
    # Fail explicitly if any future reference change attempts mel processing.
    librosa = types.ModuleType('librosa')
    filters = types.ModuleType('librosa.filters')

    def no_mel(*args, **kwargs):
        raise AssertionError('voice-clone mel outside talker reference')

    filters.mel = no_mel
    librosa.filters = filters
    sys.modules['librosa'] = librosa
    sys.modules['librosa.filters'] = filters
    for suffix in ('', '.core', '.core.models', '.inference'):
        name = 'q4t_tts_reference' + suffix
        module = types.ModuleType(name)
        module.__path__ = [str(directory)]
        sys.modules[name] = module
    wrapper = types.ModuleType('q4t_tts_reference.inference.qwen3_tts_tokenizer')

    class NoCodecInTalkerReference:
        def __init__(self, *args, **kwargs):
            raise AssertionError('codec construction outside talker reference')

        @classmethod
        def from_pretrained(cls, *args, **kwargs):
            raise AssertionError('codec loading outside talker reference')

    wrapper.Qwen3TTSTokenizer = NoCodecInTalkerReference
    sys.modules[wrapper.__name__] = wrapper
    for name, expected in SOURCES.items():
        path = directory / (name + '.py')
        assert digest(path) == expected
        spec = importlib.util.spec_from_file_location(
            'q4t_tts_reference.core.models.' + name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('assets', 'official-source', 'plan', 'native', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    os.fstat(int(os.environ['Q4T_MANAGED_LEASE_FD']))
    assert not os.environ.get('PYTHONOPTIMIZE')
    os.environ['CUDA_VISIBLE_DEVICES'] = ''
    import numpy as np
    import torch
    import transformers
    from safetensors.torch import load_file
    from transformers import Qwen2TokenizerFast
    assert transformers.__version__ == '4.57.6'
    assert not torch.cuda.is_initialized()
    torch.set_num_threads(8)
    torch.set_num_interop_threads(1)
    started = time.monotonic()

    def checkpoint():
        assert time.monotonic() - started < 600, 'TTS CPU reference deadline'

    module = import_official(args.official_source)
    assert digest(args.assets / 'model.safetensors') == (
        '38b1d5971bdbd982b561cccec982669a53b0537c3cf5e9bd4778ed07bb2f5137')
    config = module.Qwen3TTSConfig(**json.loads(
        (args.assets / 'config.json').read_text()))
    config.talker_config._attn_implementation = 'eager'
    config.talker_config.code_predictor_config._attn_implementation = 'eager'
    previous = torch.get_default_dtype()
    torch.set_default_dtype(torch.float32)
    try:
        talker = module.Qwen3TTSTalkerForConditionalGeneration(config.talker_config)
    finally:
        torch.set_default_dtype(previous)
    state = load_file(args.assets / 'model.safetensors', device='cpu')
    assert len(state) == 404 and all(key.startswith('talker.') for key in state)
    loading = talker.load_state_dict(
        {key.removeprefix('talker.'): value for key, value in state.items()},
        strict=True, assign=False)
    assert not loading.missing_keys and not loading.unexpected_keys
    del state
    talker.eval()
    assert all(p.dtype == torch.float32 and p.device.type == 'cpu'
               for p in talker.parameters())
    assert talker.model.config._attn_implementation == 'eager'
    assert talker.code_predictor.model.config._attn_implementation == 'eager'
    tokenizer = Qwen2TokenizerFast.from_pretrained(args.assets, local_files_only=True)
    # Execute official conditioning without constructing a second model or
    # permitting its codec loader. This holder only owns the loaded talker.
    holder = module.Qwen3TTSForConditionalGeneration.__new__(
        module.Qwen3TTSForConditionalGeneration)
    torch.nn.Module.__init__(holder)
    holder.config = config
    holder.talker = talker
    plan = json.loads(args.plan.read_text())
    assert len(plan['cases']) == 2
    args.output.mkdir(parents=True, exist_ok=False)
    forwards = {'talker': 0, 'predictor': 0}
    results = []
    for index, item in enumerate(plan['cases']):
        checkpoint()
        native = args.native / f'case-{index}'
        out = args.output / f'case-{index}'
        out.mkdir()

        def save(name, frame, group, tensor):
            value = tensor.detach().to(torch.float32).contiguous()
            assert value.numel() <= 512 * 2048
            (out / f'{name}-{frame}-{group}.f32').write_bytes(value.numpy().tobytes())

        captured = {}

        class Captured(Exception):
            pass

        def capture(**kwargs):
            captured.update(kwargs)
            raise Captured()

        def tokens(value):
            return torch.tensor([tokenizer.encode(value, add_special_tokens=False)])

        with torch.inference_mode(), mock.patch.object(talker, 'generate', capture):
            try:
                holder.generate(input_ids=[tokens(
                    '<|im_start|>assistant\n' + item['text'] +
                    '<|im_end|>\n<|im_start|>assistant\n')],
                    instruct_ids=[tokens('<|im_start|>user\n' +
                        item['instruction'] + '<|im_end|>\n')],
                    languages=[item['language']], speakers=[item['voice']],
                    non_streaming_mode=item['non_streaming'])
            except Captured:
                pass
            else:
                raise AssertionError('official prompt capture was bypassed')
            prepared = captured['inputs_embeds']
            save('prefill', 0, 0, prepared)
            text_ids = np.fromfile(native / 'text-ids.i32', dtype='<i4')
            assert 0 < text_ids.size <= 512 and (text_ids >= 0).all()
            assert (text_ids < 151676).all()
            save('text-projected', 0, 0, talker.text_projection(
                talker.get_text_embeddings()(torch.tensor(text_ids.astype(np.int64)))))
            codes = np.fromfile(native / 'codes.i32', dtype='<i4').reshape(2, 16)
            assert ((codes >= 0) & (codes < 2048)).all()
            cache = None
            expected = []
            first_history = []
            for frame in range(2):
                checkpoint()
                past = cache.get_seq_length() if cache is not None else 0
                output = talker.model(inputs_embeds=prepared,
                    attention_mask=torch.ones(1, past + prepared.shape[1],
                                              dtype=torch.long),
                    use_cache=True, past_key_values=cache)
                forwards['talker'] += 1
                cache = output.past_key_values
                hidden = output.last_hidden_state[:, -1:]
                logits = talker.codec_head(hidden)
                save('talker-hidden', frame, 0, hidden)
                save('talker-logits', frame, 0, logits)
                scores = logits[0, 0].float().clone()
                scores[2048:] = -float('inf')  # min_new_tokens=2
                for code in set(first_history):
                    scores[code] = (scores[code] * 1.05 if scores[code] < 0
                                    else scores[code] / 1.05)
                expected_frame = [int(scores.argmax())]
                known = torch.tensor(codes[frame].astype(np.int64)).reshape(1, 16)
                first_history.append(int(known[0, 0]))
                predictor_input = torch.cat((hidden,
                    talker.get_input_embeddings()(known[:, :1])), dim=1)
                predictor_cache = None
                for group in range(1, 16):
                    checkpoint()
                    if group == 1:
                        output = talker.code_predictor(inputs_embeds=predictor_input,
                            use_cache=True, output_hidden_states=True)
                    else:
                        output = talker.code_predictor(
                            input_ids=known[:, group-1:group], generation_steps=group-1,
                            past_key_values=predictor_cache, use_cache=True,
                            output_hidden_states=True)
                    predictor_cache = output.past_key_values
                    forwards['predictor'] += 1
                    save('predictor-hidden', frame, group,
                         output.hidden_states[-1][:, -1:])
                    save('predictor-logits', frame, group, output.logits[:, -1:])
                    expected_frame.append(int(output.logits[0, -1].float().argmax()))
                expected.append(expected_frame)
                if frame == 0:
                    embeddings = [talker.get_input_embeddings()(known[:, :1])]
                    embeddings += [talker.code_predictor.get_input_embeddings()[g-1](
                        known[:, g:g+1]) for g in range(1, 16)]
                    prepared = torch.cat(embeddings, dim=1).sum(1, keepdim=True)
                    trailing = captured['trailing_text_hidden']
                    prepared = prepared + (trailing[:, frame:frame+1]
                        if frame < trailing.shape[1] else captured['tts_pad_embed'])
        record = {'expected_greedy_codes': expected, 'teacher_forced_frames': 2,
                  'native_codes': codes.tolist(), 'eos_qualified': False}
        (out / 'result.json').write_text(json.dumps(record, indent=2) + '\n')
        results.append(record)
    assert forwards == {'talker': 4, 'predictor': 60}
    assert not torch.cuda.is_initialized()
    (args.output / 'result.json').write_text(json.dumps({
        'status': 'PASS_CPU_REFERENCE_EXECUTION_ONLY', 'cpu_model_loads': 1,
        'torch': torch.__version__, 'transformers': transformers.__version__,
        'forwards': forwards, 'seconds': time.monotonic() - started,
        'cuda_initialized': False, 'results': results}, indent=2) + '\n')


if __name__ == '__main__':
    main()

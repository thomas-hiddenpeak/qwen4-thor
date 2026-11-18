"""Exact official Qwen Base speaker classes, CPU FP32 reference under lease."""
import __future__
import argparse
import ast
import fcntl
import json
import os
from pathlib import Path
import resource
import stat
import time
import warnings

from speaker_feature_reference import definitions, digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('plan', 'native', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    lease = int(os.environ['Q4T_MANAGED_LEASE_FD'])
    assert stat.S_ISREG(os.fstat(lease).st_mode)
    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert os.environ.get('CUDA_VISIBLE_DEVICES') == ''
    import numpy as np
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
    assert torch.__version__ == '2.14.0+cpu' and np.__version__ == '2.5.2'
    torch.set_num_threads(1)
    torch.set_num_interop_threads(1)
    torch.set_float32_matmul_precision('highest')
    torch.backends.mkldnn.enabled = False
    started = time.monotonic()
    plan = json.loads(args.plan.read_bytes())
    assert len(plan['cases']) == 4
    assert all(digest(Path(p)) == h for p, h in plan['sources'].items())
    scope = dict(torch=torch, nn=nn, F=F, PretrainedConfig=object)

    def classes(path, names):
        tree = ast.parse(path.read_text(), filename=str(path))
        selected = [node for node in tree.body
                    if isinstance(node, ast.ClassDef) and node.name in names]
        assert {node.name for node in selected} == set(names)
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(path),
                     'exec', flags=__future__.annotations.compiler_flag), scope)

    official = Path(plan['official_source'])
    classes(official / 'configuration_qwen3_tts.py', ['Qwen3TTSSpeakerEncoderConfig'])
    classes(official / 'modeling_qwen3_tts.py', [
        'Res2NetBlock', 'SqueezeExcitationBlock', 'AttentiveStatisticsPooling',
        'TimeDelayNetBlock', 'SqueezeExcitationRes2NetBlock', 'Qwen3TTSSpeakerEncoder'])
    config = json.loads(Path(plan['config']).read_bytes())['speaker_encoder_config']
    assert config == dict(enc_dim=2048, sample_rate=24000)
    model = scope['Qwen3TTSSpeakerEncoder'](
        scope['Qwen3TTSSpeakerEncoderConfig'](**config)).float().eval()
    prefix = Path(plan['assets']) / 'source-prefix.bin'
    assert digest(prefix) == '6f0c979914635259a77c56b31ab9d3cda4faacc1f466ba5e26f06ce6fc8b1661'
    raw = prefix.read_bytes()
    header_size = int.from_bytes(raw[:8], 'little')
    assert header_size == 58856
    header = json.loads(raw[8:8 + header_size])
    state = {}
    for name, value in header.items():
        if not name.startswith('speaker_encoder.'):
            continue
        begin, end = value['data_offsets']
        assert 0 <= begin < end <= 24002176 and value['dtype'] == 'BF16'
        bf16 = np.frombuffer(raw, '<u2', count=(end - begin) // 2,
                             offset=8 + header_size + begin)
        values = (bf16.astype('<u4') << 16).view('<f4').reshape(value['shape'])
        assert np.isfinite(values).all()
        state[name.removeprefix('speaker_encoder.')] = torch.from_numpy(values.copy())
    assert len(state) == 76
    model.load_state_dict(state, strict=True)
    del state, raw
    scopes = []
    librosa = Path(plan['librosa_source'])
    for high_precision in (False, True):
        frontend = dict(torch=torch, np=np, warnings=warnings, ParameterError=ValueError)
        for path, names in (
            (librosa / 'convert.py', ['hz_to_mel', 'mel_to_hz', 'fft_frequencies', 'mel_frequencies']),
            (librosa / 'filters.py', ['mel']),
            (official / 'modeling_qwen3_tts.py', ['dynamic_range_compression_torch', 'mel_spectrogram'])):
            definitions(path, plan['sources'][str(path)], names, frontend, high_precision)
        frontend['librosa_mel_fn'] = frontend['mel']
        scopes.append(frontend)
    args.output.mkdir(exist_ok=False)
    records = []
    with torch.inference_mode():
        for index, case in enumerate(plan['cases']):
            assert time.monotonic() - started < 240
            directory = args.output / f'case-{index}'
            directory.mkdir()
            pcm_path = Path(case['pcm'])
            assert digest(pcm_path) == case['pcm_sha256']
            pcm = np.fromfile(pcm_path, '<f4')
            frames = case['frames']
            assert len(pcm) == case['samples'] and frames == len(pcm) // 256
            features = []
            for precision, frontend in enumerate(scopes):
                dtype = torch.float64 if precision else torch.float32
                torch.set_default_dtype(dtype)
                matrix = frontend['mel_spectrogram'](
                    torch.from_numpy(pcm)[None].to(dtype), n_fft=1024,
                    num_mels=128, sampling_rate=24000, hop_size=256,
                    win_size=1024, fmin=0, fmax=12000, center=False)[0].float()
                assert tuple(matrix.shape) == (128, frames) and torch.isfinite(matrix).all()
                features.append(matrix)
                name = 'canonical-mel' if precision else 'official-fp32-mel'
                matrix.numpy().astype('<f4').tofile(directory / (name + '.f32'))
            torch.set_default_dtype(torch.float32)
            native = np.fromfile(args.native / f'case-{index}/mel.f32', '<f4')
            assert native.size == 128 * frames and np.isfinite(native).all()
            hooks = []
            layers = [*model.blocks, model.mfa, model.asp, model.fc]
            assert len(layers) == 7
            for stage, layer in enumerate(layers):
                def save(unused_layer, unused_input, output, stage=stage):
                    value = output.detach().float().contiguous().numpy().astype('<f4')
                    assert np.isfinite(value).all()
                    value.tofile(directory / f'stage-{stage}.f32')
                hooks.append(layer.register_forward_hook(save))
            # Same actual frontend isolates native model arithmetic differences.
            result = model(torch.from_numpy(native.reshape(128, frames).T.copy())[None]).reshape(2048)
            assert torch.isfinite(result).all()
            result.numpy().astype('<f4').tofile(directory / 'embedding.f32')
            for hook in hooks:
                hook.remove()
            # Separate full official FP32 frontend impact; never hide it by
            # comparing only precomputed native inputs or rounded BF16 values.
            output = model(features[0].transpose(0, 1)[None]).reshape(2048)
            assert torch.isfinite(output).all()
            output.numpy().astype('<f4').tofile(directory / 'official-front-embedding.f32')
            records.append(dict(case=index, frames=frames, model_forwards=2))
    assert not torch.cuda.is_initialized()
    result = dict(status='PASS_REFERENCE_EXECUTED', cases=records,
        seconds=time.monotonic() - started, cpu_model_loads=1, model_forwards=8,
        cuda_initialized=False, torch=torch.__version__,
        max_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    (args.output / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()

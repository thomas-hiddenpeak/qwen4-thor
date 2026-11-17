"""Offline fixed ECAPA checkpoint conversion; PyTorch is never a serve dependency."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import time

SOURCE_SHA = '0575cb64845e6b9a10db9bcb74d5ac32b326b8dc90352671d345e2ee3d0126a2'
SOURCE_BYTES = 83316686


def convert(source, output):
    started = time.monotonic()
    if os.environ.get('CUDA_VISIBLE_DEVICES') != '':
        raise ValueError('conversion requires explicit CPU-only visibility')
    if output.exists() or source.stat().st_size != SOURCE_BYTES:
        raise ValueError('new output and exact original checkpoint required')
    if hashlib.sha256(source.read_bytes()).hexdigest() != SOURCE_SHA:
        raise ValueError('fixed original checkpoint digest differs')
    import torch
    torch.set_num_threads(1)
    state = torch.load(source, map_location='cpu', weights_only=True)
    if not isinstance(state, dict) or not 1 <= len(state) <= 512:
        raise ValueError('unexpected checkpoint tensor map')
    header, payload, tensors, offset = {}, [], [], 0
    for name, tensor in sorted(state.items()):
        if not isinstance(name, str) or not isinstance(tensor, torch.Tensor):
            raise ValueError('plain named tensors required')
        if tensor.device.type != 'cpu' or tensor.dtype not in (torch.float32, torch.int64):
            raise ValueError('unexpected device or original tensor dtype')
        if not torch.isfinite(tensor).all().item():
            raise ValueError('nonfinite original tensor')
        data = tensor.contiguous().numpy().tobytes(order='C')
        dtype = 'F32' if tensor.dtype == torch.float32 else 'I64'
        spec = dict(dtype=dtype, shape=list(tensor.shape), data_offsets=[offset, offset+len(data)])
        header[name] = spec
        tensors.append(dict(name=name, **spec, sha256=hashlib.sha256(data).hexdigest()))
        payload.append(data)
        offset += len(data)
        if offset > 84000000 or time.monotonic()-started > 120:
            raise ValueError('conversion byte/time bound exceeded')
    encoded = json.dumps(header, ensure_ascii=True, separators=(',', ':')).encode()
    encoded += b' ' * (-len(encoded) % 8)
    if len(encoded) > 65536 or torch.cuda.is_initialized():
        raise ValueError('header bound or CPU-only contract differs')
    output.mkdir(mode=0o700)
    target = output/'model.safetensors'
    with target.open('xb') as destination:
        destination.write(struct.pack('<Q', len(encoded)))
        destination.write(encoded)
        for data in payload:
            destination.write(data)
    # Check every exported byte against the tensor actually loaded above.
    with target.open('rb') as exported:
        assert exported.read(8) == struct.pack('<Q', len(encoded))
        assert exported.read(len(encoded)) == encoded
        for data in payload:
            assert exported.read(len(data)) == data
        assert not exported.read(1)
    receipt = dict(format='q4t-native-speaker-conversion-v1',
        source_sha256=SOURCE_SHA, source_bytes=SOURCE_BYTES,
        model_sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
        model_bytes=target.stat().st_size, header_bytes=len(encoded),
        weight_bytes=offset, tensors=tensors, torch_version=torch.__version__,
        tensor_values_changed=False, cuda_initialized=torch.cuda.is_initialized(),
        seconds=time.monotonic()-started)
    (output/'conversion.json').write_text(json.dumps(receipt, indent=2)+'\n')
    print(json.dumps({k:receipt[k] for k in ['model_sha256','model_bytes','header_bytes','weight_bytes','seconds']}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    convert(args.source, args.output)

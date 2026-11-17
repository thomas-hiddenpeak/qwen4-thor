"""Offline fixed SepFormer conversion. PyTorch is not a serve dependency."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import time

REVISION = '3a2826343a10e2d2e8a75f79aeab5ff3a2473531'
SOURCES = {
    'encoder': (17267,
        '3139bb880b29ea77ae8a168b8f2ad6e8eb5c2c0904289676c223d0e93cd2a35d'),
    'masknet': (113108458,
        '57dd5f49bf21c5a2101bb4e46648d05d34d517a59e26f0b06646d0bebe8214c7'),
    'decoder': (17202,
        'abea1a2d41151331b4c36071d1b3205aed940a189721f008b12a703e9c63e7e4'),
}


def convert(source, output):
    started = time.monotonic()
    if os.environ.get('CUDA_VISIBLE_DEVICES') != '':
        raise ValueError('explicit CPU-only visibility required')
    if output.exists() or shutil.disk_usage(output.parent).free < 4 << 30:
        raise ValueError('new output and at least 4 GiB free required')
    import torch
    torch.set_num_threads(1)
    named = {}
    for prefix, (size, digest) in SOURCES.items():
        path = source / (prefix + '.ckpt')
        if path.stat().st_size != size or hashlib.sha256(
                path.read_bytes()).hexdigest() != digest:
            raise ValueError('fixed original checkpoint identity differs')
        state = torch.load(path, map_location='cpu', weights_only=True)
        if not isinstance(state, dict) or not 1 <= len(state) <= 1024:
            raise ValueError('plain checkpoint tensor map required')
        for name, value in state.items():
            if (not isinstance(name, str) or not name or
                    not isinstance(value, torch.Tensor) or
                    value.device.type != 'cpu' or value.dtype != torch.float32 or
                    not torch.isfinite(value).all().item()):
                raise ValueError('finite CPU float32 named tensor required')
            named[prefix + '.' + name] = value
    header, payload, records, offset = {}, [], [], 0
    for name, tensor in sorted(named.items()):
        data = tensor.contiguous().numpy().tobytes(order='C')
        spec = dict(dtype='F32', shape=list(tensor.shape),
                    data_offsets=[offset, offset + len(data)])
        header[name] = spec
        records.append(dict(name=name, **spec,
                            sha256=hashlib.sha256(data).hexdigest()))
        payload.append(data)
        offset += len(data)
        if offset > 115000000 or time.monotonic() - started > 180:
            raise ValueError('conversion byte/time budget exceeded')
    encoded = json.dumps(header, ensure_ascii=True,
                         separators=(',', ':')).encode()
    encoded += b' ' * (-len(encoded) % 8)
    if len(encoded) > 131072 or torch.cuda.is_initialized():
        raise ValueError('header budget or CPU-only contract differs')
    output.mkdir(mode=0o700)
    target = output / 'model.safetensors'
    with target.open('xb') as destination:
        destination.write(struct.pack('<Q', len(encoded)))
        destination.write(encoded)
        for data in payload:
            destination.write(data)
    with target.open('rb') as exported:
        assert exported.read(8) == struct.pack('<Q', len(encoded))
        assert exported.read(len(encoded)) == encoded
        for data in payload:
            assert exported.read(len(data)) == data
        assert not exported.read(1)
    receipt = dict(format='q4t-native-separator-conversion-v1',
        revision=REVISION, sources={name: dict(bytes=size, sha256=digest)
            for name, (size, digest) in SOURCES.items()},
        model_sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
        model_bytes=target.stat().st_size, header_bytes=len(encoded),
        weight_bytes=offset, tensors=records, torch_version=torch.__version__,
        tensor_values_changed=False, cuda_initialized=False,
        seconds=time.monotonic() - started)
    (output / 'conversion.json').write_text(json.dumps(receipt, indent=2)+'\n')
    print(json.dumps({k: receipt[k] for k in
        ['model_sha256', 'model_bytes', 'header_bytes', 'weight_bytes', 'seconds']}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    convert(args.source, args.output)

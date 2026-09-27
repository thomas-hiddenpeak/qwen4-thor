"""Read-only audit of scalar NVFP4 scales in a safetensors checkpoint."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import struct

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--model', type=Path, required=True)
p.add_argument('--output', type=Path, required=True)
a = p.parse_args()
root = Path(__file__).resolve().parents[3]
assert any(a.output.resolve().is_relative_to(root / d) for d in ['build', '.q4t-work'])
assert not a.output.exists()
records = {}
headers = {}
invalid = []
for path in sorted(a.model.glob('*.safetensors')):
    with path.open('rb') as f:
        size = struct.unpack('<Q', f.read(8))[0]
        data = f.read(size)
        headers[path.name] = hashlib.sha256(data).hexdigest()
        for name, spec in json.loads(data).items():
            if not name.endswith(('.input_scale', '.weight_scale_2')):
                continue
            start, end = spec['data_offsets']
            if spec['dtype'] != 'F32' or end - start != 4:
                invalid.append({'name': name, 'reason': 'not scalar F32'})
                continue
            f.seek(8 + size + start)
            value = struct.unpack('<f', f.read(4))[0]
            records[name] = value
            if not math.isfinite(value) or value <= 0:
                invalid.append({'name': name, 'reason': 'nonpositive/nonfinite'})
paired = 0
for name, value in records.items():
    if '.gate_proj.' not in name:
        continue
    other = name.replace('.gate_proj.', '.up_proj.')
    if other in records:
        paired += 1
        if records[other] != value:
            invalid.append({'name': name, 'reason': 'gate/up scalar differs'})
result = {'scalar_count': len(records), 'gate_up_pairs': paired,
          'invalid': invalid, 'min': min(records.values()),
          'max': max(records.values()), 'header_sha256': headers,
          'scalars': records,
          'scope': 'Checkpoint scalar preconditions only; not runtime activations or GEMM'}
a.output.write_text(json.dumps(result, indent=2, allow_nan=False))
print({k: result[k] for k in ['scalar_count', 'gate_up_pairs', 'invalid', 'min', 'max']})
assert records and not invalid

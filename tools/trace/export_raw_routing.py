"""Export raw MoE routing from a controlled capture to readable text.

Reads request-*.bin (frame format, see analyze.py) and emits, per request, the
actual per-forward, per-layer, per-row top-k expert IDs in original order.
This is raw routing data (not aggregates); it does not add cache or throughput
claims. All inputs are read-only.

Output layout under --output:
  routing.csv          request_id,forward_id,stage,position,rows,layer,expert_ids
  requests.json        per-request metadata (http id, outcome, prompt tokens)
  manifest.json        copied run manifest (model/binary identity, dims)
  request-N.tokens.txt actual input token IDs (space-separated), one per request
"""
import argparse
import csv
import json
import shutil
import struct
import sys
import zlib
from array import array
from pathlib import Path

MAGIC = b'Q4TRTE01'
STAGE = {1: 'prefill', 2: 'decode'}
OUTCOME = {1: 'success', 2: 'cancelled', 3: 'failed'}


def frames(path):
    with path.open('rb') as file:
        if file.read(8) != MAGIC:
            raise ValueError('bad trace magic')
        while True:
            prefix = file.read(8)
            if not prefix:
                break
            if len(prefix) != 8:
                raise ValueError('truncated frame')
            size, crc = struct.unpack('<II', prefix)
            if not 0 < size <= 16 * 1024 * 1024:
                raise ValueError('invalid frame length')
            payload = file.read(size)
            if len(payload) != size or zlib.crc32(payload) != crc:
                raise ValueError('truncated/corrupt payload')
            yield payload


def export(directory, output):
    manifest = json.loads((directory / 'manifest.json').read_text())
    if manifest['schema'] != 1 or not manifest['complete']:
        raise ValueError('run incomplete: ' + manifest.get('failure', 'unknown'))
    if manifest['failure'] != 'none':
        raise ValueError('run has capture failure')
    output.mkdir(parents=True, exist_ok=True)
    shutil.copy2(directory / 'manifest.json', output / 'manifest.json')

    paths = sorted(directory.glob('request-*.bin'),
                   key=lambda p: int(p.stem.split('-')[-1]))
    if not paths:
        raise ValueError('no request-*.bin found')

    requests_meta = []
    total_ids = 0
    with (output / 'routing.csv').open('w', newline='') as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(['request_id', 'forward_id', 'stage', 'position',
                         'rows', 'layer', 'expert_ids'])
        for path in paths:
            metadata = json.loads(path.with_suffix('.json').read_text())
            events = frames(path)
            header = next(events)
            dims = struct.unpack('<IIIII', header[:20])
            if dims != (1, manifest['layers'], manifest['experts'],
                        manifest['top_k'], manifest['max_rows']):
                raise ValueError(f'{path.name}: header dims mismatch')
            request_id = None
            forward_id = None
            stage = None
            position = 0
            rows = 0
            outcome = None
            for payload in events:
                kind, _ = struct.unpack('<IQ', payload[:12])
                data = payload[12:]
                if kind == 1:
                    request_id, prompt_rows = struct.unpack('<QQ', data[:16])
                elif kind == 2:
                    forward_id, stage, position, rows = struct.unpack('<QIQI', data)
                elif kind == 3:
                    layer = struct.unpack('<I', data[:4])[0]
                    ids = array('H')
                    ids.frombytes(data[4:])
                    if sys.byteorder != 'little':
                        ids.byteswap()
                    total_ids += len(ids)
                    writer.writerow([request_id, forward_id, STAGE[stage],
                                     position, rows, layer,
                                     ','.join(str(e) for e in ids)])
                elif kind == 5:
                    outcome, _ = struct.unpack('<IQ', data)
            requests_meta.append(dict(
                file=path.name, request_id=request_id,
                http_id=metadata.get('http_id'),
                prompt_tokens=metadata.get('prompt_tokens'),
                outcome=OUTCOME.get(outcome, 'unknown')))
            # Readable input tokens (actual, little-endian uint32).
            tokens_path = path.with_suffix('.tokens')
            if tokens_path.exists():
                raw = tokens_path.read_bytes()
                toks = array('I')
                toks.frombytes(raw)
                if sys.byteorder != 'little':
                    toks.byteswap()
                (output / f'{path.stem}.tokens.txt').write_text(
                    ' '.join(str(t) for t in toks))

    (output / 'requests.json').write_text(
        json.dumps(dict(manifest=manifest, requests=requests_meta,
                        total_expert_ids=total_ids), indent=2))
    return total_ids, len(requests_meta)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--directory', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    total, nreq = export(args.directory, args.output)
    print(f'exported {nreq} requests, {total} expert ids -> {args.output}')


if __name__ == '__main__':
    main()

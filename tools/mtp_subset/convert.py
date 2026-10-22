"""Build the single 65536-row BF16 candidate, without loading a model on GPU."""
import argparse
from collections import Counter
import hashlib
import json
import mmap
from pathlib import Path
import struct


def digest(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--model', type=Path, required=True)
    p.add_argument('--calibration', type=Path, required=True)
    p.add_argument('--responses', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    # Outputs may never be placed in the checkpoint or a reference tree.
    out = a.output.resolve()
    if not any(x in ('build', '.q4t-work') for x in out.parts):
        raise ValueError('output must be under build/ or .q4t-work/')
    if out.is_relative_to(a.model.resolve()) or 'reference' in out.parts:
        raise ValueError('read-only source destination')
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(a.model, local_files_only=True)
    workload = json.loads(a.calibration.read_text())
    responses = json.loads(a.responses.read_text())
    if len(workload) != 24 or len(responses) != 24:
        raise ValueError('frozen calibration requires 24 paired records')
    counts = Counter()
    for item, response in zip(workload, responses):
        if (item['scenario'], item['variant']) != (response['scenario'], response['variant']):
            raise ValueError('calibration pairing mismatch')
        for msg in item['request']['messages']:
            counts.update(tok.encode(msg['content'], add_special_tokens=False))
        counts.update(tok.encode(response['text'], add_special_tokens=False))
    index = json.loads((a.model / 'model.safetensors.index.json').read_text())
    shard = a.model / index['weight_map']['lm_head.weight']
    with shard.open('rb') as f:
        header_len = struct.unpack('<Q', f.read(8))[0]
        tensor = json.loads(f.read(header_len))['lm_head.weight']
    vocab, hidden = tensor['shape']
    if tensor['dtype'] != 'BF16' or (vocab, hidden) != (248320, 2560):
        raise ValueError('unexpected source head')
    special = set(tok.all_special_ids)
    cfg = json.loads((a.model / 'tokenizer_config.json').read_text())
    special.update(int(k) for k, v in cfg['added_tokens_decoder'].items() if v.get('special'))
    gen = json.loads((a.model / 'generation_config.json').read_text())
    for key in ('eos_token_id', 'bos_token_id', 'pad_token_id'):
        value = gen.get(key)
        special.update(value if isinstance(value, list) else [] if value is None else [value])
    valid = set(tok.get_vocab().values())
    if not special <= valid or any(i < 0 or i >= vocab for i in valid):
        raise ValueError('tokenizer domain mismatch')
    rows = 65536
    ranking = sorted(valid - special, key=lambda i: (-counts[i], i))
    if len(special) >= rows or len(valid) < rows:
        raise ValueError('invalid special/domain capacity')
    selected = sorted(special | set(ranking[:rows - len(special)]))
    out.mkdir(parents=True, exist_ok=False)
    (out / 'token_ids.bin').write_bytes(struct.pack(f'<{rows}i', *selected))
    start = 8 + header_len + tensor['data_offsets'][0]
    with shard.open('rb') as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        with (out / 'head.bf16').open('wb') as dst:
            for i in selected:
                dst.write(mm[start + i * hidden * 2:start + (i + 1) * hidden * 2])
    sources = {str(x.resolve()): digest(x) for x in (a.calibration, a.responses)}
    manifest = dict(version=1, rows=rows, vocab=vocab, hidden=hidden, dtype='BF16',
                    source_shard=shard.name, source_sha256=digest(shard),
                    tokenizer_sha256=digest(a.model / 'tokenizer.json'),
                    tokenizer_config_sha256=digest(a.model / 'tokenizer_config.json'),
                    generation_config_sha256=digest(a.model / 'generation_config.json'),
                    head_sha256=digest(out / 'head.bf16'),
                    ids_sha256=digest(out / 'token_ids.bin'), special_ids=sorted(special),
                    calibration=sources, selection='frequency_desc_id_asc_then_rows_id_asc',
                    observed_types=len(counts), calibration_tokens=sum(counts.values()),
                    covered_tokens=sum(counts[i] for i in selected),
                    device_bytes=rows * (hidden * 2 + 4))
    (out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()

"""Bounded host checks after HTTP: source rows, IDs and load failures (including every hash field)."""
import argparse
import hashlib
import json
import mmap
from pathlib import Path
import struct
import subprocess


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('artifact', 'model', 'reader', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    manifest = json.loads((args.artifact / 'manifest.json').read_text())
    ids_raw = (args.artifact / 'token_ids.bin').read_bytes()
    ids = list(struct.unpack('<65536i', ids_raw))
    selected_ids = set(ids)
    assert ids == sorted(set(ids)) and all(0 <= i < 248320 for i in ids)
    assert set(manifest['special_ids']) <= set(ids)
    shard = args.model / manifest['source_shard']
    with shard.open('rb') as source, (args.artifact / 'head.bf16').open('rb') as target:
        header_len = struct.unpack('<Q', source.read(8))[0]
        tensor = json.loads(source.read(header_len))['lm_head.weight']
        assert tensor['dtype'] == 'BF16' and tensor['shape'] == [248320, 2560]
        start = 8 + header_len + tensor['data_offsets'][0]
        with mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            for token in ids:
                assert target.read(5120) == mm[start + token*5120:start + (token+1)*5120]
            assert not target.read(1)
    results = dict(source_rows_bit_exact=65536, special_ids=len(manifest['special_ids']), cases=[])
    for case in ('shape', 'source', 'short_ids', 'duplicate', 'range', 'unsorted',
                 'missing_special', 'ids_digest', 'short_weights', 'source_sha256',
                 'tokenizer_sha256', 'tokenizer_config_sha256',
                 'generation_config_sha256', 'head_sha256'):
        directory = args.output / case
        directory.mkdir()
        changed = dict(manifest)
        changed_ids = ids.copy()
        (directory / 'head.bf16').symlink_to((args.artifact / 'head.bf16').resolve())
        expected = 'IDs must be sorted, unique and legal'
        if case.endswith('_sha256'):
            changed[case] = '0' * 64
            expected = ('payload digest mismatch' if case == 'head_sha256'
                        else 'SHA256 mismatch')
        elif case == 'shape':
            changed['rows'] = 65535
            expected = 'unsupported format or dimensions'
        elif case == 'source':
            changed['source_shard'] = 'not-the-source.safetensors'
            expected = 'source head identity mismatch'
        elif case == 'short_ids':
            changed_ids.pop()
            expected = 'incorrect file size'
        elif case == 'duplicate':
            changed_ids[1] = changed_ids[0]
        elif case == 'range':
            changed_ids[-1] = 248320
        elif case == 'unsorted':
            changed_ids[0], changed_ids[1] = changed_ids[1], changed_ids[0]
        elif case == 'missing_special':
            removed = manifest['special_ids'][-1]
            changed_ids.remove(removed)
            replacement = next(i for i in range(248320) if i not in selected_ids)
            changed_ids = sorted(changed_ids + [replacement])
            expected = 'missing tokenizer special ID'
        elif case == 'ids_digest':
            expected = 'payload digest mismatch'
        elif case == 'short_weights':
            (directory / 'head.bf16').unlink()
            (directory / 'head.bf16').write_bytes(b'\0\0')
            expected = 'incorrect file size'
        (directory / 'token_ids.bin').write_bytes(struct.pack(f'<{len(changed_ids)}i', *changed_ids))
        changed['ids_sha256'] = '0' * 64 if case == 'ids_digest' else digest(directory / 'token_ids.bin')
        (directory / 'manifest.json').write_text(json.dumps(changed, indent=2) + '\n')
        result = subprocess.run([str(args.reader), str(directory), str(args.model)],
                                capture_output=True, text=True, timeout=120)
        (directory / 'stdout').write_text(result.stdout)
        (directory / 'stderr').write_text(result.stderr)
        passed = result.returncode == 1 and expected in result.stderr
        results['cases'].append(dict(case=case, returncode=result.returncode, passed=passed))
        (args.output / 'result.json').write_text(json.dumps(results, indent=2) + '\n')
        if not passed:
            raise RuntimeError('wrong rejection: ' + case + ': ' + result.stderr)
    print('PASS exact source rows, legal/special IDs and fourteen loader rejection cases')


if __name__ == '__main__':
    main()

"""Preserve SHA-bound inputs, original failed A and completed raw evidence."""
import hashlib
import json
from pathlib import Path
import shutil
import time

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / '.q4t-work/prefix-seed-20261028'


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    audit = json.loads((BASE / 'final-audit.json').read_text())
    if not audit['passed']:
        raise ValueError('Completed evidence audit required')
    out = BASE / 'evidence-archive-v1'
    out.mkdir()
    records, bindings = {}, {}

    def copy(source, relative, digest=None):
        source = Path(source)
        actual = sha(source)
        if digest is not None and actual != digest:
            raise ValueError('Source identity: ' + str(source))
        target = out / relative
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        if sha(target) != actual:
            raise ValueError('Copy identity: ' + str(target))
        records[str(relative)] = dict(sha256=actual, bytes=source.stat().st_size)

    for phase in audit['phases']:
        directory = BASE / phase['name']
        plan = json.loads((directory / 'plan.json').read_text())
        bindings[phase['name']] = {}
        for name, source in phase['source_bindings'].items():
            digest = plan['files'][name]
            copy(source, Path('inputs') / digest, digest)
            bindings[phase['name']][name] = digest
        for source in directory.rglob('*'):
            if source.is_file():
                if source.name == 'io-preparation.data':
                    raise ValueError('Fixed IO data still present; review ownership cleanup first')
                copy(source, Path('stages') / phase['name'] / source.relative_to(directory))
    for source in BASE.iterdir():
        if source.is_file():
            copy(source, Path('review') / source.name)
    completed = {phase['name'] for phase in audit['phases']}
    directories = ['frozen', 'fixture-revision']
    directories += [n for n in ['phase-d', 'health'] if n not in completed]
    for name in directories:
        for source in (BASE / name).rglob('*'):
            if source.is_file() and not source.is_symlink():
                copy(source, Path('preserved') / source.relative_to(BASE))
    for source in (ROOT / 'tools/prefix_seed').iterdir():
        if source.is_file():
            copy(source, Path('tools') / source.name)
    manifest = dict(files=records, bindings=bindings, created=time.time(),
        note='Original failed A retained. Historical driver resolved from '
             'preserved dc52dff source. No raw evidence rewritten or inference rerun.')
    (out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    for name, value in records.items():
        if sha(out / name) != value['sha256']:
            raise ValueError('Final archive mismatch: ' + name)
    receipt = dict(passed=True, directory=str(out), files=len(records),
        bytes=sum(r['bytes'] for r in records.values()),
        manifest_sha256=sha(out / 'manifest.json'), new_inference_requests=0)
    (BASE / 'archive-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps(receipt, indent=2))


if __name__ == '__main__':
    main()

"""Copy verified inputs and completed raw stages without an arbitrary size gate."""
import hashlib
import json
from pathlib import Path
import shutil
import time

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / '.q4t-work/session-cache-20261027'


def read(path):
    return json.loads(Path(path).read_text())


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    audit = read(BASE / 'final-audit.json')
    if not audit['passed'] or audit['cumulative_requests'] != 231:
        raise ValueError('completed pre-health evidence audit required')
    original = read(BASE / 'phase-c-fixed-cap/archive/audit.json')
    for name, digest in original['raw_file_hashes'].items():
        if sha(BASE / 'phase-c-fixed-cap' / name) != digest:
            raise ValueError('original full C raw record changed: ' + name)
    out = BASE / 'evidence-archive-v1'
    out.mkdir()
    records = {}
    sources = {}
    started = time.time()

    def copy(source, relative, expected=None):
        source = Path(source)
        if not source.is_file():
            raise ValueError('expected evidence file: ' + str(source))
        digest = sha(source)
        if expected is not None and digest != expected:
            raise ValueError('frozen input changed: ' + str(source))
        destination = out / relative
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        if sha(destination) != digest:
            raise ValueError('archive copy differs: ' + str(destination))
        records[str(relative)] = dict(sha256=digest, bytes=source.stat().st_size)
        return digest

    for phase in audit['phases']:
        name = phase['phase']
        directory = BASE / name
        plan = read(directory / 'plan.json')
        sources[name] = {}
        for path, source in audit['binding_sources'][name].items():
            digest = plan['files'][path]
            copy(source, Path('inputs') / digest, digest)
            sources[name][path] = digest
        for source in sorted(directory.rglob('*')):
            relative = source.relative_to(directory)
            if source.is_file() and (relative.parts[0] in ['run', 'lease'] or
                                     len(relative.parts) == 1):
                copy(source, Path('stages') / name / relative)
        historical = directory / 'archive'
        if historical.exists():
            for source in sorted(historical.glob('*.json')):
                copy(source, Path('stages') / name / 'prior-audit' / source.name)
    for name in ['final-audit.json', 'performance-gate.json',
                 'protection-comparisons.json', 'resources-d.json',
                 'resources-c-full.json', 'resources-c-subset.json',
                 'resources-c-original.json', 'ram-allocation-accounting.json',
                 'recovery-adjudication.json', 'suffix-work-summary.json']:
        copy(BASE / name, Path('review') / name)
    for source in sorted((BASE / 'evidence').glob('*')):
        if source.is_file():
            copy(source, Path('build-and-driver-logs') / source.name)
    copy(Path(__file__), Path('review') / 'archive_evidence.py')
    manifest = dict(files=records, input_sources=sources,
        original_full_c_raw_hashes_verified=len(original['raw_file_hashes']),
        raw_statuses={p['phase']: p['original_state'] for p in audit['phases']},
        note='Original paths and failures retained. Full C includes a later '
             'derived CPU adjudication file; every original raw hash is unchanged.')
    (out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    actual = {str(p.relative_to(out)) for p in out.rglob('*') if p.is_file()}
    if actual != set(records) | {'manifest.json'}:
        raise ValueError('archive file set differs')
    for name, record in records.items():
        if sha(out / name) != record['sha256']:
            raise ValueError('final archive hash differs: ' + name)
    receipt = dict(passed=True, directory=str(out), files=len(records),
        bytes=sum(r['bytes'] for r in records.values()),
        manifest_sha256=sha(out / 'manifest.json'),
        original_full_c_raw_hashes_verified=len(original['raw_file_hashes']),
        elapsed_seconds=time.time() - started, new_inference_requests=0)
    with (BASE / 'archive-receipt.json').open('x') as stream:
        json.dump(receipt, stream, indent=2)
        stream.write('\n')
    print(json.dumps(receipt, indent=2))


if __name__ == '__main__':
    main()

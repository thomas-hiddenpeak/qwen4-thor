"""Archive completed or stopped evidence, with hashes and no log-size gate."""
import hashlib
import json
from pathlib import Path
import shutil
import time

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT/'.q4t-work/mtp-prefill-release-20261029'
OUT = BASE.with_name(BASE.name+'-archive')


def read(path):
    return json.loads(Path(path).read_text())


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    decision = read(BASE/'final-decision.json')
    if decision['passed']:
        if not read(BASE/'final-audit-health.json')['passed']:
            raise ValueError('Delivered evidence requires final health audit')
        if read(BASE/'phase-h/finalized.json')['status'] != 'DELIVERED':
            raise ValueError('Installation not finalized')
    else:
        read(BASE/'partial-audit.json')
    for path in BASE.glob('phase-*/run/state.json'):
        if read(path)['status'] == 'RUNNING':
            raise ValueError('Never archive live measurement files')
    if list(BASE.glob('phase-*/io-preparation.data')):
        raise ValueError('Review and release owned IO preparation first')
    started = time.time()
    OUT.mkdir()
    records, bindings = {}, {}

    def copy(source, relative, expected=None):
        source, relative = Path(source), Path(relative)
        digest = sha(source)
        if expected is not None and digest != expected:
            raise ValueError('Bound input differs: '+str(source))
        target = OUT/relative
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        if sha(target) != digest:
            raise ValueError('Archive copy differs: '+str(target))
        records[str(relative)] = dict(sha256=digest, bytes=source.stat().st_size)

    # Keep original paths in bindings, including unexecuted frozen plans.
    for plan_path in sorted(BASE.glob('phase-*/plan.json')):
        bindings[plan_path.parent.name] = {}
        for path, digest in read(plan_path)['files'].items():
            source = Path(path)
            # A failed install may have restored the default pair. Retain the
            # tested candidate from its sealed package, without rewriting the
            # original health plan or disguising the rollback in its audit.
            if (source.parent == ROOT/'build' and
                    source.name in ['q4t', 'q4t.release.json'] and
                    sha(source) != digest):
                source = BASE/'delivery-package/candidate'/source.name
            copy(source, Path('inputs')/digest, digest)
            bindings[plan_path.parent.name][path] = dict(
                sha256=digest, preserved_source=str(source))
    for source in sorted(BASE.rglob('*')):
        if source.is_file():
            if source.is_symlink():
                raise ValueError('Unexpected evidence symlink: '+str(source))
            copy(source, Path('raw')/source.relative_to(BASE))
    for source in sorted((ROOT/'tools/mtp_prefill_release').glob('*.py')):
        copy(source, Path('release-tools')/source.name)
    prior = ROOT/'.q4t-work/mtp-prefill-scoring-20261029'
    for name in ['review.json', 'archive-receipt.json']:
        copy(prior/name, Path('reused-research')/name)
    manifest = dict(files=records, bindings=bindings, created=time.time(),
        candidate_sha256=decision['candidate_sha256'],
        engineering_decision=decision['engineering_decision'],
        note='Original raw statuses and preparation failures preserved; no '
             'measurement rewritten, repeated, or excluded for log size.')
    (OUT/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    actual = {str(p.relative_to(OUT)) for p in OUT.rglob('*') if p.is_file()}
    if actual != set(records) | {'manifest.json'}:
        raise ValueError('Archive file set differs')
    for name, record in records.items():
        if sha(OUT/name) != record['sha256']:
            raise ValueError('Final archive hash differs: '+name)
    receipt = dict(passed=True, directory=str(OUT), files=len(records),
        bytes=sum(r['bytes'] for r in records.values()),
        manifest_sha256=sha(OUT/'manifest.json'),
        elapsed_seconds=time.time()-started, new_gpu_http_requests=0)
    with (BASE/'archive-receipt.json').open('x') as stream:
        json.dump(receipt, stream, indent=2)
        stream.write('\n')
    print(json.dumps(receipt, indent=2))


if __name__ == '__main__':
    main()

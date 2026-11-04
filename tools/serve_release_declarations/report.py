"""Seal and verify SRV-17 metadata evidence without model, HTTP or GPU work."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
from tools.serve_release_declarations import stage as s
from tools.serve_service import package as p

DEST = ROOT/'docs/evidence/serve-release-declarations-20261104'
EXCLUDED = {'previous/q4t', 'prepared-backend/q4t',
            'prepared-backend/client-policy.json'}


def seal():
    s.check()
    delivery = p.read(s.BASE/'delivery.json')
    p.require(delivery['status'] == 'DELIVERED_METADATA_ONLY', 'not delivered')
    p.require(p.sha(ROOT/'build/q4t') == s.ELF and
              p.sha(ROOT/'build/q4t.release.json') == delivery['sidecar']['sha256'],
              'current pair changed')
    p.verify_declarations(p.read(ROOT/'build/q4t.release.json'), required=True)
    receipt = p.read(s.BASE/'prepared.json')
    p.verify(receipt['package'], receipt['manifest_sha256'])
    p.verify(s.OLD_PACKAGE, s.MANIFEST)
    for name, status in (('cpu/result.json', 'PASS_CPU'),
                         ('cpu/offline.json', 'PASS_OFFLINE'),
                         ('real-regression.json', 'PASS_REAL_STALE_REJECTION'),
                         ('isolated-entry.json', 'PASS_PACKAGED_ENTRY'),
                         ('postflight.json', 'PASS_POSTFLIGHT')):
        p.require(p.read(s.BASE/name)['status'] == status, 'missing gate: '+name)
    s.snapshot(Path(__file__).resolve(), s.BASE/'report-source.py')
    DEST.mkdir(exist_ok=False)
    records, excluded = [], []
    secret = p.read(s.BASE/'prepared-backend/client-policy.json')['gateway_token'].encode()
    with tarfile.open(DEST/'records.tar.gz', 'w:gz') as archive:
        for path in sorted(s.BASE.rglob('*')):
            if path.is_dir():
                continue
            p.regular(path)
            name = str(path.relative_to(s.BASE))
            if name in EXCLUDED:
                excluded.append(s.identity(path))
                continue
            raw = path.read_bytes()
            p.require(secret not in raw, 'private gateway credential in evidence')
            record = dict(name=name, **s.identity(path))
            records.append(record)
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            info.mode = 0o400
            info.mtime = 0
            archive.addfile(info, io.BytesIO(raw))
    p.require({str(Path(r['path']).relative_to(s.BASE)) for r in excluded} == EXCLUDED,
              'unexpected evidence exclusions')
    p.save(DEST/'manifest.json', dict(schema=1, records=records,
        local_only=excluded, archive_sha256=p.sha(DEST/'records.tar.gz')))
    p.save(DEST/'result.json', dict(status='ACCEPT_METADATA_DECLARATIONS',
        binary_sha256=s.ELF, previous_sidecar_sha256=s.SIDECAR,
        sidecar_sha256=delivery['sidecar']['sha256'],
        prepared_manifest_sha256=receipt['manifest_sha256'],
        prepared_service_runtime_accepted=False,
        cpu_tests=p.read(s.BASE/'cpu/result.json')['tests'], schemas=7,
        stale_sections=4, stale_schema_digests=3, runtime_sources_unchanged=222,
        records=len(records), archive_bytes=(DEST/'records.tar.gz').stat().st_size,
        archive_sha256=p.sha(DEST/'records.tar.gz'),
        original_harness_failure_preserved=True, model_starts=0, http_requests=0,
        gpu_tests=0, cli_version_checks=1, long_tests_run=False,
        shared_lock_inode=3294067, restored_service_masked=True))


def verify():
    manifest, result = (p.read(DEST/n) for n in ('manifest.json', 'result.json'))
    p.require(p.sha(DEST/'records.tar.gz') == manifest['archive_sha256'] ==
              result['archive_sha256'], 'archive digest differs')
    with tarfile.open(DEST/'records.tar.gz', 'r:gz') as archive:
        members = archive.getmembers()
        p.require(len(members) == len(manifest['records']) == result['records']
                  and len({m.name for m in members}) == len(members),
                  'archive inventory count differs')
        expected = {row['name']: row for row in manifest['records']}
        raw = {}
        for member in members:
            p.require(member.isfile() and member.name in expected and
                      member.size <= 2*1024**2 and not member.name.startswith('/')
                      and '..' not in Path(member.name).parts, 'unsafe member')
            data = archive.extractfile(member).read()
            record = expected[member.name]
            p.require(hashlib.sha256(data).hexdigest() == record['sha256'] and
                      len(data) == record['bytes'], 'record digest differs')
            raw[member.name] = data
    read = lambda name: json.loads(raw[name])
    candidate = read('candidate/q4t.release.json')
    p.require(hashlib.sha256(raw['candidate/q4t.release.json']).hexdigest() ==
              result['sidecar_sha256'] and candidate['binary_sha256'] == s.ELF,
              'candidate identity differs')
    p.require(hashlib.sha256(raw['previous/q4t.release.json']).hexdigest() ==
              s.SIDECAR, 'previous sidecar differs')
    scratch = ROOT/'build/release-declarations-audit'
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch) as tmp:
        target = Path(tmp)
        for name in p.DECLARATION_FILES:
            dest = target/name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(raw['declarations/'+name])
        p.verify_declarations(candidate, target, required=True)
    cpu = read('cpu/result.json')
    p.require(cpu['status'] == 'PASS_CPU' and cpu['tests'] == result['cpu_tests']
              == len(set(cpu['names'])) == 62 and not cpu['failures'] and
              not cpu['errors'] and not cpu['skipped'], 'CPU evidence differs')
    p.require(read('isolated-invocation-01.json')['status'] == 'FAIL_HARNESS_IMPORT'
              and read('isolated-entry.json')['status'] == 'PASS_PACKAGED_ENTRY'
              and read('postflight.json')['lock']['available'] is True,
              'invocation or cleanup evidence differs')
    p.require(len(read('runtime-source.json')) == 222 and
              read('delivery.json')['model_starts'] == 0 and
              read('delivery.json')['http_requests'] == 0, 'scope differs')
    return dict(status='PASS_ARCHIVE', records=len(raw),
                sidecar_sha256=result['sidecar_sha256'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('seal', 'verify'))
    args = parser.parse_args()
    if args.action == 'seal':
        seal()
    print(json.dumps(verify()))

"""Offline, content-qualified rollback copy for a ctime-only metadata change."""
import argparse
import copy
import hashlib
import inspect
import os
from pathlib import Path
import shutil
import stat
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import bundle, ledger
from tools.serve_service import package as engine

BUNDLE = 'source/tools/serve_recovery/bundle.py'
CONTROLLER = 'source/tools/serve_recovery/controller.py'
FIELDS = ('dev', 'ino', 'size', 'mtime_ns', 'ctime_ns')


def drift(expected, current):
    ledger.require(expected.keys() == current.keys(), 'model asset inventory differs')
    changed = {}
    for name, old in expected.items():
        new = current[name]
        ledger.require(set(old) == set(new) == set(FIELDS) and
                       all(old[k] == new[k] for k in FIELDS if k != 'ctime_ns'),
                       'content qualification only covers ctime drift')
        if old != new:
            changed[name] = new
    ledger.require(changed, 'model has no ctime drift')
    return changed


def read_bound(path, digest, name):
    raw = bundle.ingress.read_file(path, maximum=2*1024**2)
    ledger.require(hashlib.sha256(raw).hexdigest() == digest, name+' identity differs')
    return engine.decode_metadata(raw)


def source_index(path, digest, repository, revision):
    index = read_bound(path, digest, 'upstream index')
    ledger.require(index['id'] == repository, 'upstream model repository differs')
    return index_rows(index, revision)


def index_rows(index, revision):
    ledger.require(index['sha'] == revision and type(revision) is str and
                   len(revision) == 40 and all(c in '0123456789abcdef' for c in revision),
                   'upstream immutable model revision differs')
    rows = {row['rfilename']:row for row in index['siblings']}
    ledger.require(len(rows) == len(index['siblings']), 'duplicate upstream asset')
    return rows


def content_hash(model, name, wanted):
    path = model/name
    ledger.require(path.resolve(strict=True) == path and path.is_relative_to(model),
                   'noncanonical model dependency')
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    def check():
        info = os.fstat(fd)
        ledger.require(stat.S_ISREG(info.st_mode) and
                       all(wanted[k] == getattr(info, 'st_'+k) for k in FIELDS),
                       'model asset changed during verification')
    with os.fdopen(fd, 'rb', buffering=0) as stream:
        check()
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        check()
    ledger.require(bundle.file_identity(path) == wanted, 'model asset path changed')
    return digest


def statement(package, digest):
    ledger.require(engine.sha(package/'manifest.json') == digest, 'retained manifest changed')
    manifest = engine.read(package/'manifest.json')
    ledger.require('model_ctime_binding' not in manifest, 'use the original sealed package')
    core = engine.verify(package/'engine', manifest['engine_manifest_sha256'])
    model = Path(core['model_dir']).resolve(strict=True)
    return manifest, model, bundle.assets(model)


def validated_binding(package, digest, report, report_sha, index_sha, rows):
    manifest, model, current = statement(package, digest)
    expected = manifest['model_assets']
    changed = drift(expected, current)
    observed = read_bound(report, report_sha, 'content report')
    ledger.require(observed['status'] == 'PASS' and observed['retained_manifest_sha256'] == digest
        and observed['previous_engine_sha256'] == engine.sha(package/'engine/q4t')
        and observed['upstream_receipt_sha256'] == index_sha and
        set(observed['assets']) == set(changed), 'content report scope differs')
    for name, new in changed.items():
        row, remote = observed['assets'][name], rows[name]
        h = remote['lfs']['sha256']
        ledger.require(type(h) is str and len(h) == 64 and all(c in '0123456789abcdef' for c in h) and
            new['size'] == remote['size'] == remote['lfs']['size'] and row['matched'] is True and
            row['sha256'] == row['expected_sha256'] == h and row['current'] == new and
            row['sealed'] == expected[name], 'reviewed asset identity/content differs')
    binding = dict(format='q4t-model-ctime-content-v1', original_manifest=digest,
                   sealed_inventory=ledger.digest(expected), observed_inventory=ledger.digest(current),
                   content_report=report_sha, upstream_index=index_sha)
    # Check the original launcher, sources, evidence, policies, assets and
    # engine using this authenticated observation. Ordinary verify stays strict.
    bundle._verify(package, digest, reviewed_binding=binding)
    return manifest, binding


def replace_once(text, old, new):
    ledger.require(text.count(old) == 1, 'unknown retained management source')
    return text.replace(old, new)


def adapter_sources(package, manifest):
    def source(name):
        data = (package/name).read_bytes()
        ledger.require(hashlib.sha256(data).hexdigest() == manifest['files'][name],
                       'retained source changed before adaptation')
        return data.decode('utf-8')
    original, controller = source(BUNDLE), source(CONTROLLER)
    anchor = 'def check_assets(model, expected, *, read_edges=False):'
    if 'def observed_assets(' not in original:
        original = replace_once(original, anchor, inspect.getsource(bundle.observed_assets)+'\n\n'+anchor)
        keys = "        'files', 'python', 'model_assets', 'weight_shards_rehashed', 'resource_mode',"
        original = replace_once(original, keys, keys+"\n        *(('model_ctime_binding',) if 'model_ctime_binding' in value else ()),")
        line = "    check_assets(Path(base['model_dir']), value['model_assets'])"
        original = replace_once(original, line, "    model = Path(base['model_dir'])\n    check_assets(model, observed_assets(model, value))")
    if 'bundle.observed_assets(' not in controller:
        old = "            bundle.check_assets(Path(self.base['model_dir']), self.manifest['model_assets'], read_edges=True)"
        controller = replace_once(controller, old, "            model = Path(self.base['model_dir'])\n            bundle.check_assets(model, bundle.observed_assets(model, self.manifest), read_edges=True)")
    return {BUNDLE:original, CONTROLLER:controller}


def verify_copy(original, digest, selected, report, upstream):
    """Accept only the reproducible metadata adapter for the sealed origin."""
    binding = selected['manifest']['model_ctime_binding']
    ledger.require(binding['original_manifest'] == digest and
                   binding['content_report'] == report['sha256'] and
                   binding['upstream_index'] == upstream['sha256'],
                   'selected rollback and model evidence differ')
    audit = read_bound(report['path'], report['sha256'], 'content report')
    index = read_bound(upstream['path'], upstream['sha256'], 'upstream index')
    rows = index_rows(index, audit['revision'])
    manifest, expected_binding = validated_binding(
        original, digest, report['path'], report['sha256'], upstream['sha256'], rows)
    expected = copy.deepcopy(manifest)
    expected['model_ctime_binding'] = expected_binding
    for name, text in adapter_sources(original, manifest).items():
        expected['files'][name] = hashlib.sha256(text.encode()).hexdigest()
    ledger.require(engine.same_json(expected, selected['manifest']),
                   'content-qualified rollback changed another package contract')


def prepare(package, digest, report, report_sha, index_sha, rows, output):
    original, binding = validated_binding(package, digest, report, report_sha, index_sha, rows)
    replacements = adapter_sources(package, original)
    ledger.require(not output.resolve().is_relative_to(package), 'output inside retained package')
    target = engine.private_directory(output, Path(original['repository']))
    shutil.copytree(package, target, dirs_exist_ok=True, symlinks=True)
    updated = copy.deepcopy(original)
    updated['model_ctime_binding'] = binding
    for name, text in replacements.items():
        path = target/name
        path.chmod(0o600)
        path.write_text(text)
        path.chmod(0o400)
        updated['files'][name] = engine.sha(path)
    engine.save(target/'manifest.json', updated)
    selected = engine.sha(target/'manifest.json')
    launcher = target/'bin/q4t'
    launcher.chmod(0o700)
    launcher.write_bytes(bundle.launcher(selected, updated['python']['path']))
    launcher.chmod(0o500)
    bundle.verify(target, selected)
    ledger.require(engine.sha(package/'manifest.json') == digest, 'original package changed')
    return dict(package=str(target), manifest_sha256=selected, original_manifest_sha256=digest,
                engine_sha256=engine.sha(target/'engine/q4t'), binding=binding)


def publication_proof(value):
    """Exact immutable inputs frozen into a STOPPED publication request."""
    from tools.serve_recovery import version_contract, version_policy
    ledger.keys(value, ('descriptor', 'acceptance'))
    version_contract.descriptor(value['descriptor'])
    receipt = value['acceptance']
    ledger.keys(receipt, ('release', 'decision', 'content_report', 'upstream_index'))
    for item in receipt.values():
        ledger.keys(item, ('path', 'sha256'))
        version_policy.hash_value(item['sha256'])
        path = item['path']
        ledger.require(type(path) is str and len(path) <= 4096 and
                       Path(path).is_absolute(), 'invalid model proof receipt path')
    return value


class HistoricalModelReader:
    """Read archived registrations sharing the exact audited model inventory."""

    def __init__(self, descriptor, acceptance):
        from tools.serve_recovery import version_contract
        selected = version_contract.payload(descriptor)
        version_contract.accepted_fallback(acceptance, selected)
        ledger.require('content_report' in acceptance and 'upstream_index' in acceptance,
                       'historical model review requires a content-qualified origin')
        self.binding = selected['manifest']['model_ctime_binding']
        self.assets = selected['manifest']['model_assets']
        self.model_dir = selected['engine']['model_dir']
        self.reviewed = set()

    def verify(self, package, digest):
        package = Path(package).resolve(strict=True)
        manifest = read_bound(package/'manifest.json', digest, 'archived manifest')
        if 'model_ctime_binding' in manifest or manifest['model_assets'] != self.assets:
            return bundle.verify(package, digest)
        base = engine.verify(package/'engine', manifest['engine_manifest_sha256'])
        ledger.require(base['model_dir'] == self.model_dir,
                       'historical model directory differs from audited origin')
        checked = bundle._verify(package, digest, reviewed_binding=dict(
            self.binding, original_manifest=digest))
        self.reviewed.add(digest)
        return checked


def audit_history(state, descriptor, receipt_path, receipt_sha):
    from tools.serve_recovery import access_store, deployment, root_transfer
    state = Path(state).resolve(strict=True)
    receipt = read_bound(receipt_path, receipt_sha, 'retained model acceptance')
    reader = HistoricalModelReader(descriptor, receipt)
    with deployment.claim(state, shared=True, create=False):
        desc = deployment.read_descriptor(state)
        bound = access_store.binding(state, desc['service_id'])
        rows = root_transfer.records(bound, read_bundle=reader.verify)
    return dict(status='PASS_HISTORICAL_MODEL_REVIEW', registrations=len(rows),
        archived_manifests=sorted(reader.reviewed), receipt_sha256=receipt_sha,
        sealed_inventory=reader.binding['sealed_inventory'],
        observed_inventory=reader.binding['observed_inventory'],
        deployment_changed=False, model_starts=0)


def history_cli(arguments):
    parser = argparse.ArgumentParser(description='Read and verify retained version registrations')
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--acceptance-file', type=Path, required=True)
    parser.add_argument('--acceptance-sha256', required=True)
    args = parser.parse_args(arguments)
    descriptor = dict(package=str(args.package.resolve(strict=True)),
                      manifest_sha256=args.manifest_sha256)
    result = audit_history(args.state, descriptor, args.acceptance_file,
                           args.acceptance_sha256)
    print(ledger.canonical(result).decode())


def main():
    if sys.argv[1:2] == ['history']:
        return history_cli(sys.argv[2:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('audit','prepare'))
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--upstream-index', type=Path, required=True)
    parser.add_argument('--upstream-sha256', required=True)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--report-sha256')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    package = args.package.resolve(strict=True)
    rows = source_index(args.upstream_index, args.upstream_sha256, args.repository, args.revision)
    if args.action == 'prepare':
        ledger.require(args.report_sha256 and args.output, 'trusted audit digest and output required')
        result = prepare(package, args.manifest_sha256, args.report, args.report_sha256,
                         args.upstream_sha256, rows, args.output)
        print(ledger.canonical(result).decode())
        return
    manifest, model, current = statement(package, args.manifest_sha256)
    ledger.require(not args.report.exists() and not args.report.resolve().is_relative_to(model)
                   and not args.report.resolve().is_relative_to(package), 'fresh separate audit report required')
    files = {}
    for name, value in drift(manifest['model_assets'], current).items():
        digest = content_hash(model, name, value)
        upstream = rows[name]['lfs']
        ledger.require(upstream['size'] == value['size'] and digest == upstream['sha256'],
                       'model content differs: '+name)
        files[name] = dict(sealed=manifest['model_assets'][name], current=value,
                           expected_sha256=upstream['sha256'], sha256=digest, matched=True)
    ledger.require(bundle.assets(model) == current, 'model changed during audit')
    engine.save(args.report, dict(status='PASS', retained_manifest_sha256=args.manifest_sha256,
        previous_engine_sha256=engine.sha(package/'engine/q4t'), revision=args.revision,
        upstream_receipt_sha256=args.upstream_sha256, assets=files))
    print(engine.sha(args.report))


if __name__ == '__main__':
    main()

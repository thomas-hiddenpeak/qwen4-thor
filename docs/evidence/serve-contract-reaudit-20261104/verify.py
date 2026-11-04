"""Read-only consistency snapshot; no inference, HTTP, service or GPU calls.

Run from any directory. stdout is a JSON receipt; redirect it into .q4t-work.
This checks the current checkout, not arbitrary future release compatibility.
"""
import ast
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[3]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.serve_audit import check_inventory


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read(path):
    return json.loads(path.read_text())


def require(value, message):
    if not value:
        raise ValueError(message)


def archive(name):
    folder = ROOT / 'docs/evidence' / name
    manifest = read(folder / 'manifest.json')
    require(sha(folder / 'records.tar.gz') == manifest['archive_sha256'],
            name + ': archive changed')
    require(sha(folder / 'result.json') == manifest['result_sha256'],
            name + ': result changed')
    rows = manifest['records']
    expected = (rows if isinstance(rows, dict) else
                {row['artifact']: row['sha256'] for row in rows
                 if 'artifact' in row})
    seen = set()
    with tarfile.open(folder / 'records.tar.gz', 'r:gz') as stream:
        for item in stream:
            require(item.isfile() and item.name not in seen and
                    item.name in expected and item.size <= 64 * 1024**2,
                    name + ': unexpected/duplicate member')
            seen.add(item.name)
            with stream.extractfile(item) as source:
                digest = hashlib.file_digest(source, 'sha256').hexdigest()
            require(digest == expected[item.name], name + ': member changed')
    require(seen == expected.keys(), name + ': missing members')
    return dict(path=str(folder.relative_to(ROOT)),
                archive_sha256=manifest['archive_sha256'],
                result_sha256=manifest['result_sha256'],
                records=len(seen), result_status=read(folder / 'result.json')['status'])


def main():
    release_path = ROOT / 'build/q4t.release.json'
    release = read(release_path)
    binary = sha(ROOT / 'build/q4t')
    require(binary == release['binary_sha256'] ==
            '61abe09fb0d2585c4f8efa3e5740fa4d0ea5af06971e4490df2d17000453ecce',
            'default binary changed')
    require(sha(release_path) ==
            'ce925d517adee1b7dfdc17c8c90b11e4bd06260e5a87701b6e6268a0fb0329fc',
            'default release changed')
    runtime_path = Path(release['tested_source_manifest'])
    require(sha(runtime_path) == release['tested_source_manifest_sha256'],
            'runtime manifest changed')
    runtime = read(runtime_path)
    require(len(runtime) == 223, 'runtime inventory changed')
    for name, digest in runtime.items():
        require(sha(ROOT / name) == digest, 'runtime changed: ' + name)
    inventory = read(check_inventory.INVENTORY)
    actual = check_inventory.current_surface(ROOT)
    require(not check_inventory.check(inventory, actual), 'native surface drift')

    package = ROOT / '.q4t-work/serve-supervisor-deadlines-20261104/backend-package'
    manifest_path = package / 'manifest.json'
    require(sha(manifest_path) ==
            'fcff7218de3b8616d641d3e8780077ca577d3ba9e012350a7723390031428240',
            'managed manifest changed')
    manifest = read(manifest_path)
    for name, digest in manifest['files'].items():
        require(sha(package / name) == digest, 'managed file changed: ' + name)
        if name.startswith('source/'):
            require(sha(ROOT / name.removeprefix('source/')) == digest,
                    'managed workspace source changed: ' + name)

    module = ast.parse((ROOT / 'tools/project_tasks/engine.py').read_text())
    runtime_files = next(ast.literal_eval(node.value) for node in module.body
                         if isinstance(node, ast.Assign) and
                         any(isinstance(target, ast.Name) and
                             target.id == 'RUNTIME_FILES' for target in node.targets))
    readers = read(ROOT / 'docs/evidence/serve-task-startup-protocol-20261104/manifest.json')['readers']
    task_sources = {name: sha(ROOT / name) for name in runtime_files}
    require(len(task_sources) == 17 and
            all(digest == readers[name] for name, digest in task_sources.items()),
            'application source changed')
    old = subprocess.check_output(
        ['git', 'show', '93d4fac4:docs/APPLICATION_SERVICE.md'], cwd=ROOT)
    history = (ROOT / 'docs/APPLICATION_SERVICE_HISTORY_2026-11-04.md').read_bytes()
    require(history.split(b'\n\n', 1)[1] == old, 'history body changed')
    routes = list(inventory['routes'])
    matrix = (ROOT / 'docs/APPLICATION_SERVICE.md').read_text()
    require(all(route.split(' ', 1)[1] in matrix for route in routes),
            'route absent from matrix')
    evidence = [archive(name) for name in (
        'serve-startup-listeners-20261104',
        'serve-supervisor-deadlines-20261104',
        'serve-task-startup-protocol-20261104')]
    days = [ROOT / '.q4t-work' / name / 'day/plan.json' for name in (
        'serve-soak-20261103',
        'serve-cuda-quota-integration-20261104/operator-soak',
        'serve-clients-managed-20261104')]
    print(json.dumps(dict(
        scope='static_identity_and_documentation_only',
        reviewed_commit='93d4fac4',
        runtime_tests_executed=False, models_started=0, http_requests=0,
        gpu_lease_acquired=False, semantic_completeness_certified=False,
        verifier_sha256=sha(Path(__file__)),
        default_binary_sha256=binary, sidecar_sha256=sha(release_path),
        protocol_sha256=release['chat_protocol']['protocol_sha256'],
        native_surface=dict(fields=len(actual['fields']), routes=routes,
                            whitelists=len(actual['whitelists']),
                            source_anchors=len(actual['sources_sha256'])),
        runtime_sources_count=len(runtime), runtime_manifest_sha256=sha(runtime_path),
        application_sources=task_sources,
        managed_package=dict(manifest_sha256=sha(manifest_path),
                             files=len(manifest['files']), settings=manifest['settings']),
        previous_matrix_sha256=hashlib.sha256(old).hexdigest(),
        preserved_day_plans={str(path.relative_to(ROOT)): sha(path) for path in days},
        archives=evidence, archived_members=sum(row['records'] for row in evidence)),
        ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

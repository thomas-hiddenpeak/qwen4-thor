"""Seal the directed SRV-12g compatibility review before registry changes."""
import difflib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
from tools.serve_service import package as pkg
from tools.project_tasks import engine

BASE = ROOT/'.q4t-work/serve-task-operations-20261104'
DEST = ROOT/'docs/evidence/serve-task-operations-review-20261104'
OLD = ROOT/'.q4t-work/serve-operations-listener-20261104/package-business-fix/previous'
EVIDENCE = ROOT/'docs/evidence/serve-operations-listener-20261104'
CHANGED = ('include/q4t/server/chat_server.h',
    'include/q4t/server/http_connection.h', 'include/q4t/server/server_options.h',
    'src/main.cpp', 'src/server/chat_capabilities.cpp', 'src/server/chat_http.cpp',
    'src/server/chat_metrics.cpp', 'src/server/chat_plan.cpp',
    'src/server/chat_server.cpp', 'src/server/server_options.cpp')
BEFORE = 'fc63db28261a37ead3b633e0cd704a128e91b4eac264ea8125f715ac4b2834aa'
AFTER = 'ddfba8c002d8fb899b4c00d42612b0a26669036a1acc65e8292a10c0a9b69342'
TEMPLATE = 'c3cf9e34abf4f9e36c2d72165aa9c132d3e2a725b6c2586aaa3a8af9d7a81041'
HISTORY = 'docs/evidence/serve-task-protocol-20261104/records.tar.gz'
HISTORY_SHA = 'adf2b620aa84a6a63fa193b1ca452e9566f07f1d591d39ad62a799bf62150b32'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def build():
    os.umask(0o077)
    BASE.mkdir(mode=0o700, exist_ok=True)
    out = BASE/'review'
    out.mkdir(mode=0o700)
    data = {}
    def add(name, raw):
        pkg.require(name not in data, 'duplicate review record')
        data[name] = raw
    metadata = {}
    sources = {}
    formatted = {}
    for label, directory, protocol in (('old', OLD, BEFORE),
                                       ('new', ROOT/'build', AFTER)):
        sidecar = directory/'q4t.release.json'
        m = pkg.read(sidecar)
        pkg.require(pkg.sha(directory/'q4t') == m['binary_sha256'] and
                    m['chat_protocol']['protocol_sha256'] == protocol and
                    m['chat_protocol']['template']['reference_sha256'] == TEMPLATE,
                    'release identity drift')
        metadata[label] = m
        add(label+'/q4t.release.json', sidecar.read_bytes())
        manifest = Path(m['tested_source_manifest'])
        pkg.require(pkg.sha(manifest) == m['tested_source_manifest_sha256'],
                    'runtime manifest changed')
        sources[label] = pkg.read(manifest)
        add(label+'/runtime-source.json', manifest.read_bytes())
        for name, expected in sources[label].items():
            raw = subprocess.check_output(['git', 'show', m['source_commit']+':'+name], cwd=ROOT)
            pkg.require(sha(raw) == expected, 'git source not accepted bytes: '+name)
            if name in CHANGED:
                add(label+'/source/'+name, raw)
                result = subprocess.run(['clang-format', '--style=file',
                    '--assume-filename='+str(ROOT/name)], input=raw,
                    capture_output=True, timeout=10, check=True)
                formatted[label, name] = result.stdout.decode()
    actual = sorted(n for n in sources['old'].keys() | sources['new'].keys()
                    if sources['old'].get(n) != sources['new'].get(n))
    pkg.require(actual == list(CHANGED) and len(sources['old']) == len(sources['new']) == 222,
                'unreviewed runtime change')
    marker = 'std::string EffectiveServeConfigJson('
    before, after = (formatted[label, 'src/server/chat_plan.cpp'] for label in ('old', 'new'))
    pkg.require(before.count(marker) == after.count(marker) == 1 and
                before.split(marker)[0] == after.split(marker)[0],
                'request planning changed beyond formatting')
    for name in CHANGED:
        diff = ''.join(difflib.unified_diff(formatted['old', name].splitlines(True),
            formatted['new', name].splitlines(True), fromfile='old/'+name, tofile='new/'+name))
        add('reviewed-diff/'+name+'.diff', diff.encode())
    pkg.require(metadata['old']['chat_protocol']['template'] ==
                metadata['new']['chat_protocol']['template'], 'template declaration changed')
    # The existing accepted archive predates the new registry and prevents a
    # circular checksum dependency when registering this reader as index 7.
    pkg.require(pkg.sha(ROOT/HISTORY) == HISTORY_SHA, 'historical archive changed')
    runtime = engine.sources()
    with tarfile.open(ROOT/HISTORY) as archive:
        for name, digest in runtime.items():
            found = [m for m in archive.getmembers() if m.isfile() and
                     m.name.endswith('/source/'+name) and
                     sha(archive.extractfile(m).read()) == digest]
            pkg.require(found, 'accepted application source not found: '+name)
    add('historical-runtime.json', json.dumps(runtime, indent=2, sort_keys=True).encode()+b'\n')
    manifest = pkg.read(EVIDENCE/'manifest.json')
    selected = []
    for row in manifest['records']:
        artifact = row.get('artifact', '')
        # Existing acceptance includes public Chat, true project tools,
        # capability/config discovery, cancellation, and all preserved failures.
        if artifact.startswith('records/execution-04/business/'):
            path = EVIDENCE/artifact
            pkg.require(pkg.sha(path) == row['sha256'], 'accepted business evidence changed')
            add('accepted/'+artifact, path.read_bytes())
            selected.append(artifact)
    pkg.require(selected, 'no accepted business records')
    for name in ('result.json', 'manifest.json'):
        add('accepted/'+name, (EVIDENCE/name).read_bytes())
    add('review-source.py', Path(__file__).read_bytes())
    proof = dict(from_protocol=BEFORE, to_protocol=AFTER, template_sha256=TEMPLATE,
        runtime_count=222, unchanged_runtime_count=212, changed=actual,
        request_planning_same_after_formatting=True, formatted_with=str(Path('/usr/bin/clang-format').resolve()),
        formatter_sha256=pkg.sha(Path('/usr/bin/clang-format').resolve()),
        template_unchanged=True, historical_runtime_files=len(runtime),
        historical_archive=HISTORY, historical_archive_sha256=HISTORY_SHA,
        old_binary_sha256=metadata['old']['binary_sha256'],
        new_binary_sha256=metadata['new']['binary_sha256'],
        prior_business_successes=5, prior_real_tools=2, reused_business_records=len(selected),
        reviewed_scope='Public project-readonly Chat/preflight and saved tool history. '
            'New private operations routing/metrics/config are not wire-identical. '
            'No automatic migration, endpoint move, reverse or transitive approval.',
        new_model_tests=0, new_http_requests=0)
    add('review.json', json.dumps(proof, indent=2, sort_keys=True).encode()+b'\n')
    for name, raw in data.items():
        path = out/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    DEST.mkdir()
    with tarfile.open(DEST/'records.tar.gz', 'w:gz') as archive:
        for name, raw in sorted(data.items()):
            member = tarfile.TarInfo(name)
            member.mode = 0o400
            member.size = len(raw)
            archive.addfile(member, io.BytesIO(raw))
    pkg.save(DEST/'manifest.json', dict(records={n: sha(raw) for n,raw in sorted(data.items())},
        archive_sha256=pkg.sha(DEST/'records.tar.gz'), review=proof))
    print(json.dumps(dict(records=len(data), archive_sha256=pkg.sha(DEST/'records.tar.gz'))))


if __name__ == '__main__':
    build()

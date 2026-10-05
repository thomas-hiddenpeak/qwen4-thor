"""One-shot delivery packaging after final documents and closed audits.

No project-module imports, subprocesses, model operations, Git writes or cleanup.
Inputs stay intact. New output paths are exclusive; an interrupted attempt is
retained and must not be rerun over the same paths. Review this script before use.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

R = Path(__file__).resolve().parent
W = R / 'source'
TAG = 'offload-decode-log-20261005'
PACKET = W / 'docs/evidence' / TAG
SCRIPTS = W / 'tools/evalscope/experiments/decode_log_20261005'
RAW_MANIFEST = R / 'raw-evidence-manifest.json'
RECEIPT = R / 'delivery-preparation.json'
START = R / 'delivery-preparation-start.json'
GROUPS = ('quality-c', 'history-a', 'history-b', 'history-c',
          'matrix-a', 'matrix-c')
COMMIT = '6b5169355d7aa666653c3b7076676e64f697d313'
BINARY = '8447d8982ba705518be23f255ddf8dc402e4feb1dbe9f42536acd9634cbb3aa6'
PLAN_SHA = '4814e3f1e0581fe18335f950a814b315e08711e277e764807f28d5afb1349fb2'
PHASE_SHA = 'ecfcd41ef3e6dd5ff9387f34a06c43e1c1f524cf01e3cc2f120ad8a519f0fd01'
HASHED = {}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def signature(path):
    value = path.lstat()
    require(stat.S_ISREG(value.st_mode), 'nonregular input: ' + str(path))
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)


def digest(path):
    path = Path(path)
    require(path.is_absolute(), 'absolute input required')
    require(not path.is_symlink(), 'symlink input refused: ' + str(path))
    before = signature(path)
    if path in HASHED:
        require(HASHED[path]['stat'] == before, 'input changed: ' + str(path))
        return HASHED[path]['sha256']
    with path.open('rb') as stream:
        value = hashlib.file_digest(stream, 'sha256').hexdigest()
    require(signature(path) == before, 'input changed while hashing')
    HASHED[path] = {'sha256': value, 'bytes': before[2], 'stat': before}
    return value


def read(path):
    path = Path(path)
    require(path.stat().st_size <= 8 << 20, 'JSON metadata bound exceeded')
    digest(path)
    value = json.loads(path.read_text())
    require(signature(path) == HASHED[path]['stat'], 'JSON changed while read')
    return value


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x') as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write('\n')


def local_file(value):
    path = Path(value).absolute()
    require(path.parent == R and not path.is_symlink() and path.is_file(),
            'explicit review/record must be a regular R top-level file')
    return path


def local_directory(value):
    path = Path(value).absolute()
    require(path.parent == R and path.is_dir() and not path.is_symlink(),
            'execution directory must be a real R top-level directory')
    require(path.name not in ('source', 'candidate-source', 'candidate-build',
                             'request-host-build'), 'build/source tree refused')
    return path


def files_under(directory):
    require(directory.is_dir() and not directory.is_symlink(), 'invalid tree')
    result = []
    for base, directories, files in os.walk(directory, followlinks=False):
        require(all(not (Path(base) / name).is_symlink() for name in directories),
                'symlink directory refused')
        for name in files:
            path = Path(base) / name
            signature(path)
            result.append(path)
    return sorted(result)


def has_digest(value, expected):
    if isinstance(value, dict):
        return any(has_digest(item, expected) for item in value.values())
    if isinstance(value, list):
        return any(has_digest(item, expected) for item in value)
    return value == expected


def passed_review(path, anchors):
    review = read(path)
    require(any(review.get(key) is True for key in
                ('review_passed', 'passed_scoped_review', 'passed')),
            'independent review is not passing: ' + str(path))
    require(not review.get('blocking_findings'), 'review has blocking findings')
    require(all(has_digest(review, value) for value in anchors),
            'review does not bind the current source/plan: ' + str(path))
    return review


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self-sha256', required=True)
    parser.add_argument('--matrix-review', required=True, type=local_file)
    parser.add_argument('--resource-review', required=True, type=local_file)
    parser.add_argument('--resource-summary', required=True, type=local_file)
    parser.add_argument('--report-review', required=True, type=local_file)
    parser.add_argument('--protection-execution-dir', required=True,
                        type=local_directory)
    parser.add_argument('--resource-execution-dir', required=True,
                        type=local_directory)
    parser.add_argument('--record', action='append', default=[], type=local_file,
                        help='Additional reviewed small R metadata, never raw payload')
    parser.add_argument('--source', action='append', default=[], type=local_file,
                        help='Additional actually executed R Python audit source')
    parser.add_argument('--completion-date', default='2026-10-05')
    args = parser.parse_args()
    require(re.fullmatch(r'\d{4}-\d{2}-\d{2}', args.completion_date), 'invalid date')
    require(digest(Path(__file__).absolute()) == args.self_sha256, 'self SHA mismatch')
    require(all(not path.exists() and not path.is_symlink() for path in
                (PACKET, SCRIPTS, RAW_MANIFEST, RECEIPT, START)),
            'output exists; preserve prior attempt, never overwrite')
    require(digest(R / 'execution-plan.json') == PLAN_SHA and
            digest(R / 'plan.json') == PHASE_SHA, 'wrong frozen plans')
    plan = read(R / 'execution-plan.json')
    require(plan['runtime_source_commit'] == COMMIT and
            plan['runtime_binary_sha256'] == BINARY, 'wrong runtime identity')
    pipeline = read(R / 'pipeline-execution/exit.json')
    require(pipeline['completed'] is True and pipeline['failure'] is None and
            len(pipeline['completed_steps']) == 17 and
            pipeline['plan_sha256'] == PLAN_SHA and
            pipeline['runtime_source_commit'] == COMMIT and
            pipeline['runtime_binary_sha256'] == BINARY,
            'pipeline not fully closed')
    protection = read(R / 'final-protection.json')
    require(protection['scoped_checks_passed'] is True and
            protection['failure'] is None and
            protection['pinned_source_sha256']['execution-plan.json'] == PLAN_SHA,
            'final protection did not pass for this phase')
    require(digest(R / 'check_final_protection.py') ==
            protection['checker_sha256'], 'protection checker identity changed')
    resource = read(R / 'raw-resource-audit.json')
    require(resource['audit_complete'] is True and
            resource['all_summary_recomputations_match'] is True and
            resource['all_identity_contracts_satisfied'] is True and
            resource['selected_groups'] == list(GROUPS) and
            resource['http_request_count'] == 110 and
            resource['client_envelope_count'] == 100 and
            not resource.get('failure'), 'raw resource audit incomplete')
    resource_sha = digest(R / 'raw-resource-audit.json')
    passed_review(args.matrix_review, [PLAN_SHA, digest(R / 'matrix-decision.json'),
                                      digest(R / 'performance-decision.json')])
    passed_review(args.resource_review, [PLAN_SHA, resource_sha])
    summary = read(args.resource_summary)
    require(has_digest(summary, resource_sha), 'resource summary lacks raw binding')
    performance = read(R / 'performance-decision.json')
    history = read(R / 'history-decision.json')
    require(performance['decision'] == 'NO_GO_PERFORMANCE' and
            performance['evidence_contracts_passed'] is True and
            history['passed'] is False and
            history['history_failure_is_permanent_veto'] is True,
            'this final package is only for the fixed history-veto NO_GO')
    not_triggered = read(R / 'conditional-stages-not-triggered.json')
    require(has_digest(not_triggered, digest(R / 'history-decision.json')) and
            has_digest(not_triggered, digest(R / 'performance-decision.json')),
            'conditional disposition does not bind the final veto/decision')
    require(set(p.name for p in (W / '.q4t-work/evidence').iterdir()) == set(GROUPS),
            'unexpected or incomplete evidence group set')
    docs = [W / 'docs/OFFLOAD_DECODE_LOG_2026-10-05.md', W / 'docs/STATUS.md',
            W / 'docs/README.md', W / 'docs/log/README.md',
            W / ('docs/log/' + args.completion_date + '.md')]
    article = docs[0].read_text()
    require('NO_GO' in article and 'NOT_TRIGGERED' in article and
            not re.search(r'\bPENDING\b|_PENDING|【待补】|草稿尚未交付', article),
            'topic report not finalized')
    passed_review(args.report_review, [digest(docs[0])])
    for group in GROUPS:
        stage = read(R / (group + '-stage.json'))
        require(stage['cleanup_complete'] is True and stage['failure'] is None
                and stage['returncode'] == 0 and stage['plan_sha256'] == PLAN_SHA,
                'group is not closed: ' + group)
    for directory in (args.protection_execution_dir, args.resource_execution_dir):
        exit_record = read(directory / 'exit.json')
        require(exit_record.get('returncode', exit_record.get('rc')) == 0 and
                not exit_record.get('failure'), 'offline audit invocation failed')

    names = ['goal-objective.json', 'plan.json', 'execution-plan.json', 'entry.json',
             'candidate-source-identity.json', 'build-identity.json',
             'build-attempt.json', 'implementation-commit.json',
             'execution-admission.json', 'first-test-admission.json',
             'runtime-static-review.json', 'root-static-tool-review.json',
             'quality-decision.json', 'host-decision.json', 'host-first-attempt.json',
             'numerical-decision.json', 'numerical-first-attempt.json',
             'completed-contracts-review.json', 'history-decision.json',
             'history-bc-diagnostic.json', 'history-results-review.json',
             'matrix-decision.json', 'performance-decision.json',
             'residual-path-note.json', 'conditional-delivery-preparation.json',
             'conditional-stages-not-triggered.json', 'final-protection.json',
             'check-final-protection-review.json', 'required-numerical-tests.txt']
    records = {R / name for name in names}
    records.update((args.matrix_review, args.resource_review,
                    args.resource_summary, args.report_review, *args.record))
    for group in GROUPS:
        records.update(R / (group + ending) for ending in
                       ('-command.json', '-controller-start.json',
                        '-controller-exit.json', '-stage.json'))
        if group != 'quality-c':
            records.add(R / (group + '-decision.json'))
        base = W / '.q4t-work/evidence' / group
        records.update(base / name for name in
                       ('wrapper-exit.json', 'runner-process-group.json',
                        'http/exit.json', 'http/capacity.json',
                        'http/isolation/identity.json',
                        'http/isolation/cleanup.json'))
    execution_dirs = [R / 'pipeline-execution', R / 'host-01', R / 'numerical-01',
                      args.protection_execution_dir, args.resource_execution_dir]
    raw = set()
    for directory in execution_dirs:
        values = files_under(directory)
        raw.update(values)
        records.update(p for p in values if p.suffix == '.json' and
                       ('start' in p.name or 'exit' in p.name or
                        p.name == 'numerical-identity-before.json'))
    scripts = [R / name for name in (
        'build_candidate.py', 'freeze_execution.py', 'phase_common.py',
        'run_stage.py', 'run_direct_contracts.py', 'audit_group.py',
        'compare_groups.py', 'run_pipeline.py', 'audit_resources.py',
        'check_final_protection.py', 'prepare_delivery.py')]
    scripts = sorted(set(scripts + args.source))
    require(all(path.suffix == '.py' for path in scripts), 'source must be Python')
    for path in scripts:
        if str(path) in plan['frozen_files']:
            require(digest(path) == plan['frozen_files'][str(path)],
                    'executed frozen controller changed')
    for group in GROUPS:
        raw.update(files_under(W / '.q4t-work/evidence' / group))
        raw.add(R / (group + '-controller.log'))
    # Only these individual build objects are read; never walk any build tree.
    build_files = [R / name for name in (
        'candidate-source.tar', 'candidate-build/q4t',
        'candidate-build/q4t_tests', 'candidate-build/CMakeCache.txt',
        'candidate-build/compile_commands.json',
        'request-host-build/q4t_request_policy_contracts',
        'build.log', 'configure.log')]
    raw.update(records)
    raw.update(scripts)
    raw.update(build_files)
    raw.update(R / name for name in
               ('entry-main.diff', 'model-entry.json', 'raw-resource-audit.json'))
    require(digest(R / 'candidate-build/q4t') == BINARY, 'runtime changed')
    numeric = read(R / 'numerical-decision.json')
    require(digest(R / 'candidate-build/q4t_tests') ==
            numeric['test_binary_sha256_after'], 'test binary changed')
    for path in raw:
        require(path.is_relative_to(R), 'raw inventory escaped phase root')
        digest(path)
    for path in records:
        require(path.suffix in ('.json', '.txt') and path.stat().st_size <= 1 << 20,
                'record is not bounded metadata: ' + str(path))
        require(path.name not in ('model-entry.json', 'entry-main.diff',
                    'raw-resource-audit.json', 'protocol.json', 'responses.json',
                    'results.json', 'requests.jsonl'), 'raw payload cannot enter Git')
    for doc in docs:
        digest(doc)
    # Canonical tools are already tracked at COMMIT; do not copy their tree.
    dependencies = []
    for name, expected in plan['tool_sha256'].items():
        path = W / 'tools/evalscope' / name
        require(digest(path) == expected, 'canonical dependency changed')
        dependencies.append({'repository_path': str(path.relative_to(W)),
                             'commit': COMMIT, 'sha256': expected})
    core = Path(resource['method']['core_path'])
    require(digest(core) == resource['method']['core_sha256'], 'resource core changed')
    for path, value in HASHED.items():
        require(signature(path) == value['stat'], 'input changed before packaging')

    save(START, {'started_t': time.time(), 'script_sha256': args.self_sha256,
                 'automatic_retry': False, 'candidate_accepted': False})
    copies = {}
    for source in sorted(records):
        evidence = W / '.q4t-work/evidence'
        relative = (Path('groups') / source.relative_to(evidence)
                    if source.is_relative_to(evidence) else source.relative_to(R))
        copies[source] = PACKET / 'records' / relative
    for source in scripts:
        copies[source] = SCRIPTS / 'originals' / source.name
    for source, destination in copies.items():
        destination.parent.mkdir(parents=True, exist_ok=True)
        with source.open('rb') as incoming, destination.open('xb') as outgoing:
            while chunk := incoming.read(1 << 20):
                outgoing.write(chunk)
        require(digest(destination) == digest(source), 'archive copy differs')
    entries = []
    for path in sorted(raw):
        rel = path.relative_to(R)
        group = next((g for g in GROUPS if g in rel.parts or
                      path.name.startswith(g + '-')), 'phase')
        entries.append({'original_root_alias': 'R', 'relative_path': str(rel),
                        'bytes': HASHED[path]['bytes'], 'sha256': digest(path),
                        'stage': group, 'artifact_type': path.suffix.lstrip('.') or 'binary',
                        'execution_status': ('PREPARATION_ONLY_NOT_EXECUTED'
                            if path.name == 'conditional-delivery-preparation.json'
                            else 'SOURCE_SNAPSHOT_SEE_EXECUTION_RECEIPTS'
                            if path.suffix == '.py' else 'CLOSED_PHASE_EVIDENCE'),
                        'owner_or_terminal_source': (
                            'source/.q4t-work/evidence/' + group + '/wrapper-exit.json'
                            if group in GROUPS else 'pipeline-execution/exit.json'),
                        'archive_path': (str(copies[path].relative_to(W))
                                         if path in copies else None)})
    save(RAW_MANIFEST, {'schema': 1, 'created_t': time.time(),
        'original_roots': {'R': str(R)}, 'entries': entries,
        'file_count': len(entries), 'total_bytes': sum(p['bytes'] for p in entries),
        'scope': 'Six closed HTTP trees; named executed controller/direct/audit '
                 'receipts/logs; named build objects only. No model/reference/source/'
                 'build whole-tree traversal. No deletion, compression or deduplication.',
        'external_frozen_bindings': plan['frozen_files'],
        'external_binding_scope': 'Referenced expected SHA values retained from the '
                 'frozen plan and checked by final-protection; external trees are '
                 'not embedded or scanned by this packager.',
        'excluded': ['this manifest', 'later generated package payloads',
                     'packaging receipts', 'postcommit receipts', 'preterminal drafts']})
    save(PACKET / 'raw-evidence-index.json', {
        'schema': 1, 'raw_manifest_path': str(RAW_MANIFEST),
        'raw_manifest_sha256': digest(RAW_MANIFEST),
        'raw_manifest_bytes': RAW_MANIFEST.stat().st_size,
        'file_count': len(entries), 'total_bytes': sum(p['bytes'] for p in entries),
        'raw_manifest_entries_embedded': False, 'raw_data_backup': False,
        'limit': 'Full raw data remain local. SHA values cannot reconstruct missing '
                 'logs, databases, resources, binaries or external model dependencies.'})
    save(PACKET / 'dependencies.json', {'schema': 1, 'runtime_source_commit': COMMIT,
        'runtime_binary_sha256': BINARY, 'canonical_tools': dependencies,
        'external_resource_core': {'path': str(core), 'sha256': digest(core)},
        'fixtures': 'Exact existing prompt paths/SHA and tokenizer/model dependencies '
                    'remain in records/execution-plan.json; prompts are not copied.',
        'archived_sources_executable_in_place': False,
        'limit': 'One-shot scripts bind absolute original directories and some execute '
                 'at import time. Inspect archived bytes; never import or rerun them '
                 'in place. A new experiment needs newly frozen paths and commands.'})
    with (PACKET / 'README.md').open('x') as stream:
        stream.write('# Decode-log evidence — NO_GO\n\n'
            'This packet accompanies the [final report](../../OFFLOAD_DECODE_LOG_2026-10-05.md). '
            'History remains a permanent veto; conditional delivery and a second '
            'candidate were not triggered. Archiving is not acceptance.\n\n'
            'The tested source is `' + COMMIT + '`. The later archive commit is '
            'a separate identity, recorded by local post-commit receipts.\n\n'
            '[package-manifest.json](package-manifest.json) binds final documents '
            'and all small packet payloads. Its [checksum](package-manifest.sha256) '
            'is detached; both and later receipts are excluded from its own entries.\n\n'
            '[Raw index](raw-evidence-index.json) binds the full local manifest; raw '
            'entries/logs/resources/databases/build objects are not embedded. This '
            'Git packet is not a raw-data or model backup.\n\n'
            '[Dependencies](dependencies.json) records tracked tools and external '
            'fixtures/core. [Controller source snapshots]('
            '../../../tools/evalscope/experiments/decode_log_20261005/) are exact '
            'bytes, non-portable and unsafe to rerun in place. All JSON archive paths '
            'resolve from the repository root.\n')
    with (SCRIPTS / 'README.md').open('x') as stream:
        stream.write('# Decode-log phase source snapshots\n\n'
            'Exact original bytes of executed build/freeze/pipeline/audit/packaging '
            'sources. They bind absolute original paths and one-shot outputs; some '
            'run at import time. Do not import or execute archived copies.\n\n'
            'Canonical tools remain tracked at the tested commit. See '
            '[dependencies](../../../../docs/evidence/' + TAG + '/dependencies.json) '
            'and the [evidence packet](../../../../docs/evidence/' + TAG + '/README.md).\n')
    payload = sorted(set(docs + files_under(PACKET) + files_under(SCRIPTS)))
    reverse = {destination: source for source, destination in copies.items()}
    final_entries = [{'repository_path': str(path.relative_to(W)),
        'bytes': path.stat().st_size, 'sha256': digest(path),
        'origin': str(reverse[path]) if path in reverse else None}
        for path in payload]
    for path, value in HASHED.items():
        require(signature(path) == value['stat'], 'input/payload changed while packaging')
    manifest = PACKET / 'package-manifest.json'
    save(manifest, {'schema': 1, 'runtime_source_commit': COMMIT,
        'runtime_binary_sha256': BINARY, 'candidate_disposition': 'NO_GO_PERFORMANCE',
        'entries': final_entries, 'file_count': len(final_entries),
        'exclusions': ['package-manifest.json itself', 'package-manifest.sha256',
                       'R delivery-preparation*.json and all postcommit receipts'],
        'raw_evidence_backup': False, 'self_contained_small_payload_inventory': True})
    final_sha = digest(manifest)
    with (PACKET / 'package-manifest.sha256').open('x') as stream:
        stream.write(final_sha + '  package-manifest.json\n')
    save(RECEIPT, {'schema': 1, 'ended_t': time.time(), 'completed': True,
        'script_sha256': args.self_sha256, 'copied_originals': len(copies),
        'raw_files': len(entries), 'packet_payload_files': len(final_entries),
        'raw_manifest_sha256': digest(RAW_MANIFEST),
        'package_manifest_sha256': final_sha,
        'candidate_accepted': False, 'commit_or_push_performed': False,
        'postcommit_receipts_required': ['delivery-audit.json', 'final-completion-audit.json']})
    print(json.dumps({'prepared': True, 'copied_originals': len(copies),
                      'raw_files': len(entries), 'package_sha256': final_sha}))


if __name__ == '__main__':
    main()

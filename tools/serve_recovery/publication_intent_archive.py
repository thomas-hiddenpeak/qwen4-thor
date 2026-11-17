"""Reversibly retire obsolete publication intents under STOPPED service locks.

Current history and pre-journal requests stay online. An archived request cannot
be resumed online until restored; a stale original publish still fails its
predecessor check. No ledger, recovery budget, package, or generation is reset.
"""
import argparse
import os
from pathlib import Path

from tools.serve_host import native
from tools.serve_recovery import ledger, publication
from tools.serve_recovery import publication_archive as archives
from tools.serve_recovery import publication_files as files
from tools.serve_service import package as engine

FORMAT = 'q4t-publication-intent-archive-v1'
MAX_SELECTED = 4

# Reviewed predecessors: original archive, then the identity-guard-only revision.
# Their plan formats and move/restore semantics are unchanged. Keep plan bytes.
LEGACY_PLAN_SOURCES = frozenset((
    '665ec99d7f46c60e750872901640660f0e34708b5d9ed058c9265b81b71b2763',
    '53a3582cc80b7049b55997caf4aa1686e03dfa1cdb1a1caf9342259cb070e865',
))


def protected(current, intents):
    publication.policy.validate(current)
    keep = {row['request_id'] for row in current['history']}
    if current['request_id'] is not None:
        keep.add(current['request_id'])
    for value in intents:
        if value['base']['revision'] >= current['revision']:
            keep.add(value['request_id'])
    return keep


def verify(path, row, state, desc):
    ledger.keys(row, ('request_id', 'sha256', 'identity', 'base_revision'))
    ledger.require(ledger.identifier(row['request_id']) and
        type(row['sha256']) is str and ledger.HASH.fullmatch(row['sha256']),
        'invalid archived request identity')
    engine.regular(path)
    ledger.require(native.inode(path) == row['identity'] and
        engine.sha(path) == row['sha256'], 'intent bytes or identity changed')
    value = publication.validate_intent(
        native.private_json(path, publication.policy.MAX_BYTES), state, desc)
    ledger.require(value['request_id'] == row['request_id'] and
        value['base']['revision'] == row['base_revision'], 'intent request differs')
    return value


def destination(output, state, manifest, current):
    root = Path(current['root'])
    ledger.require(output.is_relative_to(Path(manifest['repository'])/'.q4t-work')
        and not output.is_relative_to(state)
        and not output.is_relative_to(root/files.DIRECTORY),
        'new private archive directory required')


def prepare(state, output, requests, *, authority=None):
    ledger.require(type(requests) is list and 1 <= len(requests) <= MAX_SELECTED
        and len(set(requests)) == len(requests)
        and all(ledger.identifier(v) for v in requests),
        'one to four explicit request UUIDs required')
    with archives.locked(state, authority=authority) as (state, desc, manifest, current):
        output = Path(output).absolute()
        parent = files.directory(output.parent)
        destination(output, state, manifest, current)
        ledger.require(not os.path.lexists(output), 'new private archive directory required')
        intents = archives.intents(state, desc)
        keep = protected(current, intents)
        ledger.require(not keep.intersection(requests), 'retained intent cannot be archived')
        rows = []
        for request in sorted(requests):
            source = publication.intent_path(state, request)
            value = publication.load_intent(state, desc, request)
            ledger.require(value['base']['revision'] < current['revision'],
                           'uncommitted intent cannot be archived')
            archives.unreferenced(state, current, source)
            ledger.require(source.stat().st_dev == parent.stat().st_dev,
                           'archive requires same filesystem atomic rename')
            rows.append(dict(request_id=request, sha256=engine.sha(source),
                identity=native.inode(source), base_revision=value['base']['revision']))
        value = dict(format=FORMAT, state=str(state), service_id=desc['service_id'],
            root=current['root'], archive=str(output),
            publication_revision=current['revision'],
            publication_sha256=ledger.digest(current), requests=rows,
            source_sha256=engine.sha(Path(__file__).resolve()))
        output.mkdir(mode=0o700)
        files.sync(parent)
        files.immutable_json(output/'plan.json', value)
        return dict(plan=str(output/'plan.json'), sha256=engine.sha(output/'plan.json'),
                    requests=requests, model_actions=False, files_moved=False)


def apply(path, expected, *, restore=False, authority=None):
    path = Path(path).absolute()
    ledger.require(engine.sha(path) == expected, 'reviewed archive plan changed')
    value = native.private_json(path)
    ledger.keys(value, ('format', 'state', 'service_id', 'root', 'archive',
        'publication_revision', 'publication_sha256', 'requests', 'source_sha256'))
    archive = files.directory(value['archive'])
    ledger.require(value['format'] == FORMAT and path == archive/'plan.json' and
        value['source_sha256'] in (engine.sha(Path(__file__).resolve()),
            *LEGACY_PLAN_SOURCES) and
        type(value['requests']) is list and 1 <= len(value['requests']) <= MAX_SELECTED,
        'archive plan/source identity differs')
    with archives.locked(value['state'], authority=authority) as (state, desc, manifest, current):
        destination(archive, state, manifest, current)
        ledger.require(value['service_id'] == desc['service_id'] and
            value['root'] == current['root'], 'archive service/root differs')
        if restore:
            receipt = native.private_json(archive/'retired.json')
            ledger.require(receipt.get('plan_sha256') == expected and
                receipt.get('status') == 'RETIRED', 'completed archive required')
        else:
            ledger.require(not (archive/'restored.json').exists() and
                value['publication_revision'] == current['revision'] and
                value['publication_sha256'] == ledger.digest(current),
                'publication/archive changed')
        intents = archives.intents(state, desc)
        keep = protected(current, intents)
        pool = state/publication.INTENTS
        moves, seen = [], set()
        # Validate every selected source before performing any rename.
        for row in value['requests']:
            request = row.get('request_id')
            ledger.require(ledger.identifier(request) and request not in seen,
                           'invalid or duplicate archive request')
            seen.add(request)
            source, target = publication.intent_path(state, request), archive/(request+'.json')
            if restore:
                source, target = target, source
            else:
                ledger.require(request not in keep, 'intent became retained')
                archives.unreferenced(state, current, source)
            exists = [os.path.lexists(p) for p in (source, target)]
            ledger.require(sum(exists) == 1, 'archive source/destination collision or loss')
            intent = verify(source if exists[0] else target, row, state, desc)
            ledger.require(intent['base']['revision'] < current['revision'],
                           'uncommitted intent cannot be archived or restored')
            if exists[0]:
                moves.append((source, target))
        ledger.require(not restore or len(intents)+len(moves) <= publication.MAX_INTENTS,
                       'restore exceeds active intent capacity')
        for source, target in moves:
            os.rename(source, target)
            files.sync(source.parent)
            files.sync(target.parent)
        files.sync(pool)
        files.sync(archive)
        for row in value['requests']:
            verify((pool if restore else archive)/(row['request_id']+'.json'),
                   row, state, desc)
        ledger.require(publication.ready(state, desc) == current,
                       'publication changed during archive')
        receipt = dict(status='RESTORED' if restore else 'RETIRED',
            plan_sha256=expected, publication_revision=current['revision'],
            requests=sorted(seen), current_unchanged=True, packages_unchanged=True,
            deleted_files=0, model_actions=False)
        files.immutable_json(archive/('restored.json' if restore else 'retired.json'), receipt)
        return receipt


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    create = sub.add_parser('prepare')
    create.add_argument('--state', type=Path, required=True)
    create.add_argument('--output', type=Path, required=True)
    create.add_argument('--request-id', action='append', required=True)
    for name in ('apply', 'restore'):
        command = sub.add_parser(name)
        command.add_argument('--plan', type=Path, required=True)
        command.add_argument('--sha256', required=True)
    args = parser.parse_args()
    value = prepare(args.state, args.output, args.request_id) if args.mode == 'prepare' else \
        apply(args.plan, args.sha256, restore=args.mode == 'restore')
    print(ledger.canonical(value).decode())


if __name__ == '__main__':
    main()

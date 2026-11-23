"""Reversibly archive retired version registrations under stopped ownership.

This explicit maintenance tool moves one to four immutable historical intents.
It never edits a recovery policy, refunds a budget, starts a model, or deletes
an acceptance record. Current cycles, retained publication roots and unfinished
transactions cannot be archived. Apply/restore resume the same reviewed plan.
"""
import argparse
from contextlib import contextmanager
import os
from pathlib import Path

from tools.serve_host import native
from tools.serve_recovery import access_store, bundle, ledger
from tools.serve_recovery import publication_archive as publication
from tools.serve_recovery import publication_files as files
from tools.serve_recovery import version_registration as registration
from tools.serve_recovery import version_store
from tools.serve_service import package as engine

FORMAT = 'q4t-version-archive-v2'
MAX_SELECTED = 4

# Reviewed predecessors: original archive, then the identity-guard-only revision.
# Their plan formats and move/restore semantics are unchanged. Keep plan bytes.
LEGACY_PLAN_SOURCES = frozenset((
    '03c4074d6584fe4c85fb06c9eb91ff8fd527453328acd04f1be2ed3a1a8a69e5',
    '0467795ed3a1422e1cd57c1afd22923c60288583e37477294cd9fb6f9363825e',
    '40610c3d30b67f370ce20e4e17beb107f86be69eac953795a1dac9ee52139c08',
))


@contextmanager
def locked(state, *, authority=None, read_bundle=None):
    # Also excludes live Guardians, deployment/publication writers and the GPU
    # lease. No sampling claim is needed: this tool performs no device work.
    with publication.locked(state, authority=authority, read_bundle=read_bundle) as (state, desc, manifest, current):
        binding = access_store.binding(state, desc['service_id'])
        with version_store.open_bound(binding) as writer:
            selected = writer.head['state']['policy']
            registration.for_state(state, desc, selected, read_bundle=read_bundle or bundle.verify)
            yield state, desc, manifest, current, selected, binding


def entries(state, binding, selected, *, read_bundle=None):
    result = {}
    for cycle, paths in registration.intents(state).items():
        ledger.require(set(paths) == {'committed'},
                       'unfinished version registration requires resolution')
        path = paths['committed']
        value = registration.intent(native.private_json(path,
            registration.MAX_BYTES), binding, read_bundle=read_bundle or bundle.verify)
        ledger.require(value['registration']['registry']['cycle_id'] == cycle and
            value['candidate']['revision'] <= selected['revision'],
            'uncommitted or misnamed version registration')
        result[cycle] = (path, value)
    return result


def protected(state, desc, current, selected, records):
    roots = {current['current']['manifest_sha256']}
    for row in current['history']:
        roots.update(row[k]['manifest_sha256'] for k in ('previous', 'selected'))
    for intent in publication.intents(state, desc):
        if intent['base']['revision'] >= current['revision']:
            roots.update((intent['base']['current']['manifest_sha256'],
                          intent['target']['manifest_sha256']))
    keep = {selected['registry']['cycle_id']} | {
        cycle for cycle, (_, value) in records.items()
        if value['registration']['root']['manifest_sha256'] in roots}
    from tools.serve_recovery import root_transfer
    if os.path.lexists(Path(state)/root_transfer.INTENT):
        intent = native.private_json(Path(state)/root_transfer.INTENT, root_transfer.MAX_BYTES)
        transfer = root_transfer.policy.validate(intent['transfer'])
        needed = {transfer[name]['registry']['cycle_id'] for name in ('before', 'candidate')}
        rows = {cycle: dict(value=value, sha256=engine.sha(path))
                for cycle, (path, value) in records.items() if cycle in needed}
        value = root_transfer.read_intent(access_store.binding(state, desc['service_id']), rows)
        keep.update(value['transfer'][name]['registry']['cycle_id']
                    for name in ('before', 'candidate'))
    return keep


def unreferenced(state, current, source):
    # Historical intent snapshots retain complete predecessor policy values;
    # they are evidence, not a live dependency on the original intent file.
    paths = []
    excluded = {'runs', registration.DIRECTORY,
                publication.publication.INTENTS}
    for parent, dirs, names in os.walk(state, followlinks=False):
        dirs[:] = [n for n in dirs if n not in excluded]
        paths.extend(Path(parent)/n for n in names if n.endswith('.json'))
        ledger.require(len(paths) <= 4096, 'active state reference bound')
    for row in [current['current'], *(h[k] for h in current['history']
                                     for k in ('previous', 'selected'))]:
        paths.append(files.generation(Path(current['root']), row)/
                     'service.release.json')
    total = 0
    for path in set(paths):
        total += path.stat().st_size
        ledger.require(total <= 32*1024**2, 'active state reference byte bound')
        value = native.private_json(path, 4*1024**2)
        ledger.require(not publication.references(value, str(source)),
                       'registration remains an active state/release dependency')


def verify_file(path, row, binding, *, read_bundle=None):
    ledger.keys(row, ('cycle_id', 'sha256', 'identity', 'checkpoints'))
    ledger.require(ledger.identifier(row['cycle_id']) and
        type(row['sha256']) is str and ledger.HASH.fullmatch(row['sha256']),
        'invalid archive registration identity')
    # private_json refuses symlinks, nonregular files, foreign owners and
    # writable authority. Keep the original inode as well as the full bytes.
    value = registration.intent(native.private_json(path,
        registration.MAX_BYTES), binding, read_bundle=read_bundle or bundle.verify)
    ledger.require(native.inode(path) == row['identity'] and
        engine.sha(path) == row['sha256'] and
        value['registration']['registry']['cycle_id'] == row['cycle_id'],
        'archive registration bytes or identity changed')
    return value


def checkpoint_rows(state, binding, row, value):
    from tools.serve_recovery import root_checkpoints as checkpoints
    marker = checkpoints.identity(binding, row['sha256'], row['cycle_id'])
    snapshot = checkpoints.read(state, marker)
    if snapshot is None: return []
    ledger.require(snapshot['registry'] == value['registration']['registry'],
                   'checkpoint registration changed')
    directory = Path(state)/checkpoints.DIRECTORY
    return [dict(name=row['cycle_id']+suffix, sha256=engine.sha(directory/(row['cycle_id']+suffix)),
                 identity=native.inode(directory/(row['cycle_id']+suffix)))
            for suffix in ('.bound', '.json')]


def verify_checkpoint(path, item, row, binding, value):
    from tools.serve_recovery import root_checkpoints as checkpoints
    ledger.keys(item, ('name', 'sha256', 'identity'))
    ledger.require(item['name'] in (row['cycle_id']+'.bound', row['cycle_id']+'.json') and
        engine.sha(path) == item['sha256'] and native.inode(path) == item['identity'],
        'archive checkpoint bytes or identity changed')
    marker = checkpoints.identity(binding, row['sha256'], row['cycle_id'])
    actual = native.private_json(path, checkpoints.MAX_BYTES)
    if item['name'].endswith('.bound'):
        ledger.require(actual == marker, 'archived checkpoint binding differs')
    else:
        snapshot = checkpoints.validate(actual, marker)['snapshot']
        ledger.require(snapshot['registry'] == value['registration']['registry'],
                       'archived checkpoint registry differs')


def prepare(state, output, cycles, *, authority=None, read_bundle=None):
    ledger.require(type(cycles) is list and 1 <= len(cycles) <= MAX_SELECTED and
        len(set(cycles)) == len(cycles) and all(ledger.identifier(c) for c in cycles),
        'one to four explicit version cycles required')
    with locked(state, authority=authority, read_bundle=read_bundle) as (state, desc, manifest, current, selected, binding):
        output = Path(output).absolute()
        parent = files.directory(output.parent)
        ledger.require(output.is_relative_to(Path(manifest['repository'])/'.q4t-work') and
            not output.is_relative_to(state) and
            not output.is_relative_to(Path(current['root'])/files.DIRECTORY) and
            not os.path.lexists(output), 'new private archive directory required')
        records = entries(state, binding, selected, read_bundle=read_bundle)
        ledger.require(not protected(state, desc, current, selected, records).intersection(cycles),
                       'current or retained root registration cannot be archived')
        rows = []
        for cycle in sorted(cycles):
            ledger.require(cycle in records, 'registration does not exist')
            path, value = records[cycle]
            unreferenced(state, current, path)
            ledger.require(path.stat().st_dev == parent.stat().st_dev,
                           'archive requires same filesystem atomic rename')
            row = dict(cycle_id=cycle, sha256=engine.sha(path), identity=native.inode(path),
                       checkpoints=[])
            row['checkpoints'] = checkpoint_rows(state, binding, row, value)
            verify_file(path, row, binding, read_bundle=read_bundle)
            rows.append(row)
        value = dict(format=FORMAT, state=str(state), service_id=desc['service_id'],
            archive=str(output), publication_sha256=ledger.digest(current),
            version_sha256=ledger.digest(selected), cycles=rows,
            source_sha256=engine.sha(Path(__file__).resolve()))
        output.mkdir(mode=0o700); files.sync(parent)
        files.immutable_json(output/'plan.json', value)
        return dict(plan=str(output/'plan.json'), sha256=engine.sha(output/'plan.json'),
                    cycles=cycles, model_actions=False, files_moved=False)


def apply(path, expected, *, restore=False, authority=None, read_bundle=None):
    path = Path(path).absolute()
    ledger.require(engine.sha(path) == expected, 'reviewed archive plan changed')
    value = native.private_json(path)
    ledger.keys(value, ('format', 'state', 'service_id', 'archive',
        'publication_sha256', 'version_sha256', 'cycles', 'source_sha256'))
    archive = files.directory(value['archive'])
    ledger.require(value['format'] == FORMAT and path == archive/'plan.json' and
        value['source_sha256'] in (engine.sha(Path(__file__).resolve()),
            *LEGACY_PLAN_SOURCES) and
        type(value['cycles']) is list and 1 <= len(value['cycles']) <= MAX_SELECTED,
        'archive plan/source identity differs')
    with locked(value['state'], authority=authority, read_bundle=read_bundle) as (state, desc, manifest, current, selected, binding):
        pool = state/registration.DIRECTORY
        ledger.require(value['service_id'] == desc['service_id'] and
            archive.is_relative_to(Path(manifest['repository'])/'.q4t-work') and
            not archive.is_relative_to(state) and
            not archive.is_relative_to(Path(current['root'])/files.DIRECTORY),
            'archive service identity differs')
        if not restore:
            ledger.require(not (archive/'restored.json').exists() and
                value['publication_sha256'] == ledger.digest(current) and
                value['version_sha256'] == ledger.digest(selected),
                'publication/version/archive changed')
        else:
            receipt = native.private_json(archive/'retired.json')
            ledger.require(receipt.get('plan_sha256') == expected and
                receipt.get('status') == 'RETIRED', 'completed archive required before restore')
        records = entries(state, binding, selected, read_bundle=read_bundle)
        keep = protected(state, desc, current, selected, records)
        moves, seen, saved_records = [], set(), {}
        for row in value['cycles']:
            cycle = row['cycle_id']
            ledger.require(ledger.identifier(cycle) and cycle not in seen,
                           'invalid or duplicate archive cycle')
            seen.add(cycle)
            source, target = pool/(cycle+'.json'), archive/(cycle+'.json')
            if restore: source, target = target, source
            else:
                ledger.require(cycle not in keep, 'registration became retained')
                unreferenced(state, current, source)
            exists = [os.path.lexists(p) for p in (source, target)]
            ledger.require(sum(exists) == 1, 'archive source/destination collision or loss')
            item = verify_file(source if exists[0] else target, row, binding, read_bundle=read_bundle)
            saved_records[cycle] = item
            ledger.require(item['candidate']['revision'] <= selected['revision'],
                           'restored registration is ahead of current recovery state')
            if not restore:
                # Retry after a rename must apply the same root protection as
                # a still-present entry. Do not infer safety from its absence.
                combined = dict(records, **{cycle: (source, item)})
                ledger.require(cycle not in protected(state, desc, current, selected, combined),
                               'registration root became retained')
            if exists[0]: moves.append((source, target))
            from tools.serve_recovery import root_checkpoints as checkpoints
            expected_names = [cycle+'.bound', cycle+'.json']
            ledger.require(type(row['checkpoints']) is list and
                [p['name'] for p in row['checkpoints']] in ([], expected_names),
                'archive checkpoint selection differs')
            checkpoint_pool = state/checkpoints.DIRECTORY
            if not row['checkpoints']:
                ledger.require(not any(os.path.lexists(checkpoint_pool/(cycle+s))
                    for s in ('.bound', '.json', '.bound.new', '.json.new')),
                    'checkpoint appeared after archive preparation')
            else:
                files.directory(checkpoint_pool, create=restore)
                ledger.require(not any(os.path.lexists(checkpoint_pool/(cycle+s))
                    for s in ('.bound.new', '.json.new')), 'checkpoint needs original transaction')
            for checkpoint in row['checkpoints']:
                source = checkpoint_pool/checkpoint['name']
                target = archive/('checkpoint-'+checkpoint['name'])
                if restore: source, target = target, source
                exists = [os.path.lexists(p) for p in (source, target)]
                ledger.require(sum(exists) == 1, 'checkpoint source/destination collision or loss')
                verify_checkpoint(source if exists[0] else target, checkpoint, row, binding, item)
                ledger.require((source if exists[0] else target).stat().st_dev ==
                    archive.stat().st_dev, 'checkpoint archive must use the same filesystem')
                if exists[0]: moves.append((source, target))
        restored_count = sum(not os.path.lexists(pool/(row['cycle_id']+'.json'))
                             for row in value['cycles'])
        ledger.require(not restore or len(records)+restored_count <= registration.MAX_CYCLES,
                       'restore exceeds active registration capacity')
        for source, target in moves:
            os.rename(source, target); files.sync(source.parent); files.sync(target.parent)
        files.sync(pool); files.sync(archive)
        for row in value['cycles']:
            verify_file((pool if restore else archive)/(row['cycle_id']+'.json'), row, binding, read_bundle=read_bundle)
            for checkpoint in row['checkpoints']:
                path = (state/checkpoints.DIRECTORY/checkpoint['name'] if restore else
                        archive/('checkpoint-'+checkpoint['name']))
                verify_checkpoint(path, checkpoint, row, binding, saved_records[row['cycle_id']])
        if os.path.lexists(state/checkpoints.DIRECTORY): files.sync(state/checkpoints.DIRECTORY)
        ledger.require(native.private_json(state/version_store.Store.directory_name/
            'policy.json')['state']['policy'] == selected,
                       'version policy changed during archive')
        receipt = dict(status='RESTORED' if restore else 'RETIRED', plan_sha256=expected,
            cycles=sorted(seen), version_policy_unchanged=True, current_unchanged=True,
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
    create.add_argument('--cycle', action='append', required=True)
    for name in ('apply', 'restore'):
        command = sub.add_parser(name)
        command.add_argument('--plan', type=Path, required=True)
        command.add_argument('--sha256', required=True)
    args = parser.parse_args()
    result = (prepare(args.state, args.output, args.cycle) if args.mode == 'prepare' else
              apply(args.plan, args.sha256, restore=args.mode == 'restore'))
    import json
    print(json.dumps(result))


if __name__ == '__main__': main()

"""Retain a superseded STOPPED root handoff while releasing its registration pins."""
import argparse
import json
import os
from pathlib import Path

from tools.serve_host import native
from tools.serve_recovery import ledger, maintenance, model_content
from tools.serve_recovery import publication_files as files
from tools.serve_recovery import root_checkpoints, root_transfer, version_archive
from tools.serve_service import package as engine


def completed(state, desc, selected, binding, value, entries):
    transfer = value['transfer']
    cycles = {transfer[k]['registry']['cycle_id'] for k in ('before', 'candidate')}
    ledger.require(desc.get('deployment_revision', 0) >
                   transfer['deployment']['target']['deployment_revision'] and
                   selected['revision'] > transfer['candidate']['revision'] and
                   selected['registry']['cycle_id'] not in cycles,
                   'root transfer still belongs to the current deployment or version cycle')
    rows = {cycle:dict(value=entries[cycle][1], sha256=engine.sha(entries[cycle][0]))
            for cycle in cycles if cycle in entries}
    root_transfer.validate(value, binding, rows)
    checkpoints = {}
    for field in ('before', 'candidate'):
        expected = transfer[field]
        cycle = expected['registry']['cycle_id']
        saved = root_checkpoints.read(state, root_transfer.marker(binding, rows[cycle]))
        ledger.require(saved is not None, 'completed root transfer checkpoint missing')
        root_checkpoints.extends(expected, saved)
        checkpoints[cycle] = ledger.digest(saved)
    return checkpoints


def retire(state, package, digest, output, expected, *, read_bundle=None):
    ledger.require(isinstance(expected, str) and ledger.HASH.fullmatch(expected),
                   'expected root transfer SHA-256 required')
    output = Path(output).absolute()
    parent = files.directory(output.parent)
    authority = maintenance.authority(state, package, digest, read_bundle=read_bundle)
    with version_archive.locked(state, authority=authority, read_bundle=read_bundle) as locked:
        state, desc, _, _, selected, binding = locked
        ledger.require(not output.is_relative_to(state) and not output.is_relative_to(Path(package).resolve())
                       and parent.stat().st_dev == state.stat().st_dev,
                       'archive must be outside the service and package on the same filesystem')
        source = state/root_transfer.INTENT
        archived = output/source.name
        proof = output/'retirement.json'
        if os.path.lexists(output):
            files.directory(output)
            ledger.require(output.stat().st_mode & 0o777 == 0o700 and
                           set(p.name for p in output.iterdir()) <=
                           {source.name, proof.name, proof.name+'.new'}, 'unexpected root transfer archive')
        if os.path.lexists(archived):
            value = native.private_json(archived, root_transfer.MAX_BYTES)
            ledger.require(engine.sha(archived) == expected and value['binding'] == binding,
                           'archived transfer identity differs')
            receipt = native.private_json(proof)
            ledger.require(receipt['transfer_sha256'] == expected and
                           receipt['service_id'] == desc['service_id'] and
                           receipt['request_id'] == value['transfer']['deployment']['request'],
                           'archived retirement receipt differs')
            if os.path.lexists(source):
                native.private_json(source, root_transfer.MAX_BYTES)
                ledger.require(engine.sha(source) != expected,
                               'active transfer also exists in the archive')
            files.sync(output)
            files.sync(state)
            return receipt
        ledger.require(not os.path.lexists(source.with_name(source.name+'.new')) and
                       os.path.lexists(source), 'root transfer missing or being written')
        value = native.private_json(source, root_transfer.MAX_BYTES)
        ledger.require(engine.sha(source) == expected, 'active root transfer identity differs')
        entries = version_archive.entries(state, binding, selected, read_bundle=read_bundle)
        snapshots = completed(state, desc, selected, binding, value, entries)
        if not output.exists():
            output.mkdir(mode=0o700)
            files.sync(parent)
        receipt = dict(format='q4t-retired-root-transfer-v1', service_id=desc['service_id'],
                       transfer_sha256=expected, request_id=value['transfer']['deployment']['request'],
                       checkpoints=snapshots)
        files.immutable_json(proof, receipt)
        # Only this service's locked writer can act on this private archive.
        # Atomic rename preserves the inode and the private-reader link count.
        ledger.require(not os.path.lexists(archived), 'archive destination already exists')
        os.rename(source, archived)
        files.sync(output)
        files.sync(state)
        return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('state', 'package', 'output'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--transfer-sha256', required=True)
    parser.add_argument('--model-history', type=Path)
    parser.add_argument('--model-history-sha256')
    args = parser.parse_args()
    proof = model_content.cli_publication_proof(args)
    reader = (model_content.HistoricalModelReader(proof['descriptor'], proof['acceptance']).verify
              if proof else None)
    print(json.dumps(retire(args.state, args.package, args.manifest_sha256, args.output,
                            args.transfer_sha256, read_bundle=reader)))


if __name__ == '__main__':
    main()

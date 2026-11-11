"""Separate root-owned version I/O channel; never owns core role authority.

All disk writes stay in this worker. Channel failure freezes only version
selection, and cannot issue model signals or terminate the core journal.
"""
import argparse
import os
from pathlib import Path
import socket
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import access_store, journal, ledger, store
from tools.serve_recovery import version_policy as policy, version_store


def transact(writer, request):
    ledger.keys(request, ('id', 'expected_revision', 'operation', 'args'))
    ledger.require(ledger.identifier(request['id']) and
        ledger.integer(request['expected_revision']), 'invalid version transaction')
    current = writer.head['state']['policy']
    ledger.require(current['revision'] == request['expected_revision'],
                   'version transaction predecessor changed')
    operation, args = request['operation'], request['args']
    if operation == 'resume':
        ledger.keys(args, ('boot_id',))
        ledger.require(args['boot_id'] == store.stamp().boot_id,
                       'version resume is not the current host boot')
        candidate = policy.resume(current, args['boot_id'])
    elif operation == 'observe':
        ledger.keys(args, ('event', 'proof'))
        candidate = policy.observe(current, args['event'], args['proof'], request['id'])
    elif operation == 'finish':
        ledger.keys(args, ('proof', 'binding_sha256'))
        candidate = policy.finish(current, args['proof'], args['binding_sha256'])
    else:
        raise ledger.LedgerError('version worker cannot grant role or operator authority')
    writer.transact(request['id'], request['expected_revision'], candidate)
    return dict(revision=candidate['revision'],
                result=dict(state=candidate, selection=policy.describe(candidate)))


def serve(sock, directory, service_id):
    sequence = 0
    try:
        bound = access_store.binding(directory, service_id)
        with version_store.open_bound(bound) as writer:
            value = writer.head['state']['policy']
            journal.send(sock, dict(version=journal.WIRE_VERSION, sequence=0, ok=True,
                snapshot=dict(service_id=service_id, policy=policy.FORMAT,
                              revision=value['revision'], state=value)))
            while True:
                message = journal.receive(sock)
                if message is None: return 0
                ledger.keys(message, ('version', 'sequence', 'request'))
                ledger.require(type(message['version']) is int and
                    message['version'] == journal.WIRE_VERSION and
                    ledger.integer(message['sequence'], minimum=1) and
                    message['sequence'] == sequence+1, 'version journal sequence mismatch')
                sequence = message['sequence']
                receipt = transact(writer, message['request'])
                journal.send(sock, dict(version=journal.WIRE_VERSION, sequence=sequence,
                                        ok=True, receipt=receipt))
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        try:
            journal.send(sock, dict(version=journal.WIRE_VERSION, sequence=sequence,
                ok=False, code='version_state_unavailable', recovery_frozen=True))
        except (OSError, ValueError): pass
        return 1


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fd', type=int, required=True)
    parser.add_argument('--parent-pid', type=int, required=True)
    parser.add_argument('--parent-start', type=int, required=True)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--service-id', required=True)
    args = parser.parse_args()
    with socket.socket(fileno=args.fd) as sock:
        journal.verify_parent(sock, args.parent_pid, args.parent_start)
        return serve(sock, args.directory, args.service_id)


if __name__ == '__main__': sys.exit(main())

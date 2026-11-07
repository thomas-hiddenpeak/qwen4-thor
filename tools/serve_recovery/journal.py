"""Isolated, single-writer journal over one inherited private seqpacket FD.

This is an internal Guardian worker, not a network/control API. It never
launches a model or grants authority through a status file. All blocking disk
work stays here; loss of this process freezes replacement permissions only.
"""
import argparse
import ctypes
import os
from pathlib import Path
import signal
import socket
import struct
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import ledger, store

WIRE_VERSION = 1
MAX_FRAME = 128*1024


def process_start(pid):
    fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    return int(fields[19])


def verify_parent(sock, parent_pid, parent_start):
    ledger.require(sock.family == socket.AF_UNIX and
                   sock.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) ==
                   socket.SOCK_SEQPACKET, 'private seqpacket FD required')
    peer = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET,
                                             socket.SO_PEERCRED, 12))
    ledger.require(peer[:2] == (parent_pid, os.geteuid()),
                   'journal peer identity mismatch')
    ledger.require(os.getppid() == parent_pid and
                   process_start(parent_pid) == parent_start,
                   'journal parent identity mismatch')
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        raise OSError(ctypes.get_errno(), 'journal parent-death protection')
    ledger.require(os.getppid() == parent_pid and
                   process_start(parent_pid) == parent_start,
                   'journal parent died during initialization')


def send(sock, message):
    data = ledger.canonical(message)
    ledger.require(len(data) <= MAX_FRAME, 'journal response exceeds limit')
    written = sock.send(data, socket.MSG_DONTWAIT | socket.MSG_NOSIGNAL)
    ledger.require(written == len(data), 'short journal packet')


def receive(sock):
    data, ancillary, flags, _address = sock.recvmsg(MAX_FRAME)
    if not data:
        return None
    ledger.require(not ancillary and not flags &
                   (socket.MSG_TRUNC | socket.MSG_CTRUNC),
                   'truncated or ancillary journal packet')
    return store.decode(data)


def serve(sock, directory, service_id):
    sequence = 0
    try:
        with store.Store(directory, service_id) as writer:
            send(sock, dict(version=WIRE_VERSION, sequence=sequence,
                            ok=True, snapshot=writer.state))
            while True:
                message = receive(sock)
                if message is None:
                    return 0
                ledger.keys(message, ('version', 'sequence', 'request'))
                ledger.require(type(message['version']) is int and
                    message['version'] == WIRE_VERSION and
                    ledger.integer(message['sequence'], minimum=1) and
                    message['sequence'] == sequence+1,
                    'journal version/sequence mismatch')
                sequence = message['sequence']
                reply = writer.transact(message['request'])
                send(sock, dict(version=WIRE_VERSION, sequence=sequence,
                                ok=True, receipt=reply))
    except (OSError, ledger.LedgerError) as error:
        try:
            send(sock, dict(version=WIRE_VERSION, sequence=sequence, ok=False,
                code='journal_io' if isinstance(error, OSError) else
                     'journal_contract', recovery_frozen=True))
        except (OSError, ledger.LedgerError):
            pass
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
        verify_parent(sock, args.parent_pid, args.parent_start)
        return serve(sock, args.directory, args.service_id)


if __name__ == '__main__':
    sys.exit(main())

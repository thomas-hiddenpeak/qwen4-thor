"""Private generation-bound controller channel; no application request replay."""
import os
import socket
import struct

from tools.serve_recovery import journal, ledger


def address(service_id):
    ledger.require(ledger.identifier(service_id), 'invalid service identity')
    return f'\0q4t-{os.geteuid()}-{service_id}'


def request(service_id, operation, *, timeout=3):
    ledger.require(operation in ('status', 'stop'), 'invalid control operation')
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as sock:
        sock.settimeout(timeout)
        sock.connect(address(service_id))
        peer = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET,
                                                 socket.SO_PEERCRED, 12))
        ledger.require(peer[1] == os.geteuid(), 'control peer uid mismatch')
        journal.send(sock, dict(version=1, service_id=service_id, op=operation))
        result = journal.receive(sock)
        ledger.require(type(result) is dict and result.get('version') == 1 and
                       result.get('service_id') == service_id and
                       result.get('guardian_pid') == peer[0],
                       'control reply identity mismatch')
        return result


class ControllerChannel:
    def __init__(self):
        self.service_id = os.environ['Q4T_SERVICE_ID']
        self.generation = int(os.environ['Q4T_CONTROLLER_GENERATION'])
        ledger.require(ledger.identifier(self.service_id) and
                       ledger.integer(self.generation, minimum=1),
                       'invalid controller generation')
        self.sock = socket.socket(fileno=int(os.environ['Q4T_CONTROL_FD']))
        journal.verify_parent(self.sock, int(os.environ['Q4T_GUARDIAN_PID']),
                              int(os.environ['Q4T_GUARDIAN_START']))
        self.sock.setblocking(False)
        self.sequence = 0

    def event(self, name):
        ledger.require(name in ('ready', 'heartbeat'), 'invalid control event')
        ledger.require(self.sequence < ledger.MAX_INTEGER,
                       'controller sequence exhausted')
        self.sequence += 1
        journal.send(self.sock, dict(version=1, service_id=self.service_id,
            generation=self.generation, sequence=self.sequence, event=name))

    def close(self):
        self.sock.close()

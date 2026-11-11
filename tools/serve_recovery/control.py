"""Private generation-bound controller channel; no application request replay."""
import os
import socket
import struct
import time

from tools.serve_recovery import journal, ledger

GATEWAY_REASONS = (
    'none', 'gateway_verifying', 'gateway_model_not_ready', 'gateway_stopping',
    'gateway_binding_unavailable', 'gateway_control_unavailable',
    'gateway_identity_mismatch', 'gateway_capacity_mismatch',
    'gateway_client_policy_mismatch', 'gateway_observation_unavailable',
    'gateway_observation_invalid', 'gateway_backend_not_ready',
    'gateway_generation_changed', 'gateway_client_policy_unavailable',
    'gateway_recovering')


def address(service_id):
    ledger.require(ledger.identifier(service_id), 'invalid service identity')
    return f'\0q4t-{os.geteuid()}-{service_id}'


def request(service_id, operation, *, timeout=3):
    ledger.require(operation in ('status', 'stop'), 'invalid control operation')
    return _request(service_id, operation, timeout=timeout)


def binding(service_id, *, timeout=0.5):
    """Credentials are returned only to the Guardian's pinned gateway PID."""
    return _request(service_id, 'binding', timeout=timeout)


def _request(service_id, operation, *, timeout):
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
    role = 'CONTROLLER'

    def __init__(self):
        self.service_id = os.environ['Q4T_SERVICE_ID']
        self.generation = int(os.environ[f'Q4T_{self.role}_GENERATION'])
        ledger.require(ledger.identifier(self.service_id) and
                       ledger.integer(self.generation, minimum=1),
                       'invalid controller generation')
        self.sock = socket.socket(fileno=int(os.environ['Q4T_CONTROL_FD']))
        journal.verify_parent(self.sock, int(os.environ['Q4T_GUARDIAN_PID']),
                              int(os.environ['Q4T_GUARDIAN_START']))
        self.sock.setblocking(False)
        self.sequence = 0
        self.pending = None

    def event(self, name):
        ledger.require(name in ('ready', 'heartbeat'), 'invalid control event')
        return self._send(name)

    def health(self, phase, observation, reason, *, auxiliary_io=None):
        return self._send('health', health=dict(phase=phase,
            observation=observation, reason=reason,
            **({} if auxiliary_io is None else dict(auxiliary_io=auxiliary_io))))

    def admission(self, generation, opened):
        ledger.require(ledger.integer(generation, minimum=1) and
                       type(opened) is bool, 'invalid model admission')
        return self._send('admission', admission=dict(
            model_generation=generation, opened=opened))

    def model_exit(self, generation, owner, returncode):
        """Report an actual direct-child wait result, never an inferred health error."""
        ledger.require(ledger.integer(generation,minimum=1) and type(returncode) is int and
            returncode in (-4,-6,-11), 'unclassified model exit')
        return self._send('model_exit',model_exit=dict(model_generation=generation,
            pid=owner['pid'],start_ticks=owner['start_ticks'],signal=-returncode))

    def quota_restored(self, generation, boot_id, receipt):
        ledger.require(ledger.integer(generation, minimum=2) and ledger.identifier(boot_id) and
            type(receipt) is str and ledger.HASH.fullmatch(receipt), 'invalid quota recovery receipt')
        return self._send('quota_restored', quota_restored=dict(model_generation=generation,
            source_generation=generation-1, boot_id=boot_id, receipt_sha256=receipt))

    def _send(self, name, **extra):
        ledger.require(self.sequence < ledger.MAX_INTEGER,
                       'controller sequence exhausted')
        self.sequence += 1
        journal.send(self.sock, dict(version=1, service_id=self.service_id,
            generation=self.generation, sequence=self.sequence, event=name,
            **extra))
        return self.sequence

    def begin(self, operation, args):
        ledger.require(self.pending is None, 'controller transaction pending')
        ledger.require(operation in ('authorize', 'spawned', 'ready', 'retired',
                       'fault', 'stable', 'quarantine', 'bind', 'probe'),
                       'invalid model operation')
        self.pending = self._send('transaction', transaction=dict(
            operation=operation, args=args))

    def poll(self):
        try:
            message = journal.receive(self.sock)
        except BlockingIOError:
            return None
        ledger.require(message is not None, 'Guardian channel closed')
        ledger.keys(message, ('version', 'service_id', 'generation', 'reply_to',
                              'ok', 'result'))
        ledger.require(type(message['version']) is int and
                       message['version'] == 1 and
                       message['service_id'] == self.service_id and
                       ledger.integer(message['generation'], minimum=1) and
                       message['generation'] == self.generation and
                       self.pending is not None and
                       ledger.integer(message['reply_to'], minimum=1) and
                       message['reply_to'] == self.pending and
                       type(message['ok']) is bool and
                       type(message['result']) is dict,
                       'stale or unbound controller receipt')
        self.pending = None
        return message

    def transact(self, operation, args, *, timeout=7):
        """A bounded wait that advances the actual control loop's heartbeat."""
        self.begin(operation, args)
        end = time.monotonic()+timeout
        heartbeat = 0
        while time.monotonic() < end:
            reply = self.poll()
            if reply is not None:
                return reply
            now = time.monotonic()
            if now >= heartbeat:
                self.event('heartbeat')
                heartbeat = now+0.25
            time.sleep(0.01)
        raise TimeoutError('Guardian transaction deadline expired')

    def close(self):
        self.sock.close()


class GatewayChannel(ControllerChannel):
    role = 'GATEWAY'

    def bound(self, generation):
        ledger.require(ledger.integer(generation, minimum=1),
                       'invalid model generation')
        self._send('bound', model_generation=generation)

    def unbound(self, reason=None, generation=0):
        if reason is None:
            self._send('unbound')  # Historical actor messages remain valid.
            return
        ledger.require(reason in GATEWAY_REASONS and reason != 'none' and
                       ledger.integer(generation), 'invalid binding diagnostic')
        self._send('unbound', binding_failure=dict(
            reason=reason, model_generation=generation))

    def begin(self, operation, args):
        raise ledger.LedgerError('gateway has no recovery transaction authority')

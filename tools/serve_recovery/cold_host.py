"""Native host integration for a selected resource-bound device installation.

No device association means no issuer process or cold-path dependency. Once an
association exists, damaged/missing records cannot silently choose that mode.
The detached issuer owns helper children only, never the outer/model tree.
"""
import os
from pathlib import Path
import select
import subprocess

from tools.serve_recovery import cold_authority, cold_channel, cold_issuer, device_domain
from tools.serve_recovery import device_resource, host_handoff, host_install as host
from tools.serve_recovery import ledger, resource_install as resources


def selected(value):
    """Root only; presence of any permanent/pending association selects cold."""
    ledger.require(os.geteuid() == 0, 'cold host selection requires root')
    with resources.exclusive(value['resource']['directory']) as directory:
        admin = directory/'admin'
        names = (device_resource.ASSOCIATION, device_resource.PERMANENT,
                 device_resource.ASSOCIATION+'.published')
        if not any(os.path.lexists(admin/n) or os.path.lexists(admin/(n+'.pending')) for n in names):
            return False
        association = device_domain.read_record(admin/device_resource.ASSOCIATION)
        config = device_domain.configuration(association['configuration'])
        ledger.require(config['format'] == device_resource.CONFIG_FORMAT and
            config['resource'] == value['resource'], 'cold host resource association differs')
        device_resource.check(association['device_directory'], config, directory, resources.load(directory))
        return True


def ensure(directory):
    with host.exclusive(directory) as directory:
        value = host.load(directory)
        ledger.require(value['phase'] == 'ready' and value['enabled'], 'cold host entry is not enabled')
        host_handoff.require_admission(directory, value)
        if not selected(value): return dict(selected=False, model_actions=False)
        _, root, _, _ = cold_authority.selection(directory)
    try:
        status = cold_issuer.control(directory, 'status')
        return dict(selected=True, available=status['accepting'], issuer=status, model_actions=False)
    except (OSError, ValueError, KeyError, TypeError): pass
    # No admin lock crosses the bootstrap. Its child is detached before the
    # host entry becomes the ordinary outer supervisor, so trees never mix.
    command = [value['artifact']['python']['path'], '-I', '-B',
        str(root/'tools/serve_recovery/cold_issuer.py'), '--directory', str(directory), '--daemon']
    try:
        result = subprocess.run(command, env=host.environment(), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        if result.returncode != 0: raise ValueError('cold issuer bootstrap failed')
        status = cold_issuer.control(directory, 'status')
        return dict(selected=True, available=status['accepting'], issuer=status, model_actions=False)
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        # Runtime keeps cold selected and waits; a healthy core is untouched.
        return dict(selected=True, available=False, reason='issuer_unavailable', model_actions=False)


def withdraw(directory):
    """Only requests auxiliary shutdown. A delayed worker prevents completion."""
    value = host.load(directory)
    try: sock = cold_channel.connect(value['installation_id'], timeout=2)
    except ConnectionRefusedError: return dict(issuer_listener_absent=True, model_actions=False)
    with sock:
        peer = cold_channel.credentials(sock); pidfd = os.pidfd_open(peer[0])
        try:
            try:
                cold_channel.send(sock, dict(version=1, operation='stop'))
                with cold_channel.receive(sock, count=0) as reply:
                    ledger.require(reply.result.get('ok') is True and
                        reply.result['result']['host_id'] == value['installation_id'] and
                        reply.result['result']['issuer_pid'] == peer[0], 'cold stop identity differs')
            except (BrokenPipeError, ConnectionResetError):
                # Revocation may already have made the issuer exit. Its pinned
                # kernel identity, not a missing reply, decides completion.
                pass
            ledger.require(bool(select.select([pidfd], [], [], 5)[0]),
                           'cold issuer still owns its worker slot; retry original removal')
        finally: os.close(pidfd)
    return dict(issuer_exited=True, model_actions=False)

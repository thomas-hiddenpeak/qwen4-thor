"""Native host integration for a selected resource-bound device installation.

No device association means no issuer process or cold-path dependency. Once an
association exists, damaged/missing records cannot silently choose that mode.
The detached issuer owns helper children only, never the outer/model tree.
"""
import os
from pathlib import Path
import select
import subprocess
import time

from tools.serve_recovery import cold_authority, cold_channel, cold_custody, cold_issuer, cold_watch, device_domain
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
    def snapshot(watcher):
        try:
            status = cold_issuer.control(directory, 'status')
            return dict(selected=True, available=status['accepting'], issuer=status,
                        watcher=watcher, model_actions=False)
        except (OSError, ValueError, KeyError, TypeError):
            return dict(selected=True, available=False, reason=watcher['reason'],
                        watcher=watcher, model_actions=False)
    try: return snapshot(cold_watch.control(directory, 'status'))
    except (OSError, ValueError, KeyError, TypeError): pass
    # The native watcher and all its children are auxiliary only. It is
    # detached before the host becomes the ordinary outer supervisor.
    command = [value['artifact']['python']['path'], '-I', '-B',
        str(root/'tools/serve_recovery/cold_watch.py'), '--directory', str(directory), '--daemon']
    try:
        result = subprocess.run(command, env=host.environment(), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        if result.returncode != 0: raise ValueError('cold watcher bootstrap failed')
        deadline = time.monotonic()+2
        while True:
            watcher = cold_watch.control(directory, 'status'); result = snapshot(watcher)
            if result['available'] or watcher['reason'] not in ('starting', 'running') or time.monotonic() >= deadline:
                return result
            time.sleep(.02)
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        return dict(selected=True, available=False, reason='issuer_unavailable', model_actions=False)


def custody_clear(value):
    if not selected(value): return
    with resources.exclusive(value['resource']['directory']) as directory:
        association = device_domain.read_record(directory/'admin'/device_resource.ASSOCIATION)
        config = device_domain.configuration(association['configuration'])
        fd = cold_custody.acquire(association['device_directory'], config)
        os.close(fd)


def withdraw(directory):
    """Only requests auxiliary shutdown. A delayed worker prevents completion."""
    value = host.load(directory)
    watched = cold_watch.withdraw(directory)
    try: sock = cold_channel.connect(value['installation_id'], timeout=2)
    except ConnectionRefusedError:
        custody_clear(value)
        return dict(issuer_exited=watched, issuer_listener_absent=True, model_actions=False)
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
    custody_clear(value)
    return dict(issuer_exited=True, model_actions=False)

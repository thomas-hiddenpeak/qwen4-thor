"""Protected root issuance for a validated, still-empty service generation.

Only device operations run with privileges. The selected package and ordinary
service journals are checked in a bounded UID-dropped verifier. This authority
does not stop, restart, migrate or acknowledge any model or external process.
"""
import os
from pathlib import Path
import select
import stat
import subprocess

from tools.serve_recovery import child, cold_custody, cold_requests, device_control, device_domain as domain
from tools.serve_recovery import device_resource, device_store, host_artifact, host_handoff
from tools.serve_recovery import host_install as host, journal, ledger, resource_install as resources
from tools.serve_recovery import resource_kernel as kernel, store


def selection(directory):
    """Caller holds the host admin lock; its source snapshot stays immutable."""
    value = host.load(directory)
    ledger.require(value['phase'] == 'ready' and value['enabled'] and host.public(directory) == value,
                   'cold authority host entry is not enabled')
    host_handoff.require_admission(directory, value)
    root = host_artifact.verify(directory, value['artifact'])
    with resources.exclusive(value['resource']['directory']) as resource_directory:
        current = resources.load(resource_directory); host.resource_matches(value, current)
        association = domain.read_record(resource_directory/'admin'/device_resource.ASSOCIATION)
        ledger.keys(association, ('format', 'device_directory', 'configuration_sha256', 'configuration'))
        config = domain.configuration(association['configuration'])
        ledger.require(config['format'] == device_resource.CONFIG_FORMAT,
                       'cold authority requires the original resource-bound device domain')
        device_resource.check(association['device_directory'], config, resource_directory, current)
        ledger.require(config['resource'] == value['resource'], 'cold authority resource differs')
    return value, root, Path(association['device_directory']), config


def peer_identity(peer, pidfd, selected, host_value, record):
    """The socket peer and kernel pidfd must still be the original outer owner."""
    pid, uid, gid = peer
    owner = selected['custody']['supervisor']
    ledger.require(uid == host_value['runtime_uid'] and gid == host_value['runtime_gid'] and
        pid == owner['pid'] and owner['boot_id'] == store.stamp().boot_id and
        journal.process_start(pid) == owner['start_ticks'], 'cold request peer identity differs')
    info = Path('/proc/self/fdinfo')/str(pidfd)
    fields = dict(row.split(':', 1) for row in info.read_text().splitlines() if ':' in row)
    ledger.require(fields.get('Pid', '').strip() == str(pid) and
        not select.select([pidfd], [], [], 0)[0], 'cold request peer has exited')
    status = Path('/proc')/str(pid)/'status'
    fields = dict(row.split(':', 1) for row in status.read_text().splitlines() if ':' in row)
    ledger.require(list(map(int, fields['Uid'].split())) == [uid]*4 and
        list(map(int, fields['Gid'].split())) == [gid]*4, 'cold peer credentials changed')
    ledger.require(kernel.namespaces(pid) == record['namespaces'] == kernel.namespaces(1),
                   'cold peer namespace differs')
    membership = (Path('/proc')/str(pid)/'cgroup').read_text()
    expected = '0::/'+str((Path(record['root'])/'outer').relative_to(kernel.CGROUP))+'\n'
    ledger.require(membership == expected, 'cold peer is outside the original outer leaf')
    actual = os.stat(f'/proc/{pid}/exe')
    expected = os.stat(host_value['artifact']['python']['path'])
    ledger.require((actual.st_dev, actual.st_ino) == (expected.st_dev, expected.st_ino),
                   'cold peer interpreter differs')


def verify_runtime(directory, value, root, selected, lease_fd, instance_fd):
    # The request worker is itself in a continuously owned subreaper tree.
    # If this child cannot be reaped, its slot and inherited lease stay held.
    command = [value['artifact']['python']['path'], '-I', '-B',
        str(root/'tools/serve_recovery/cold_verify.py'), '--directory', str(directory),
        '--lease-fd', str(lease_fd), '--instance-fd', str(instance_fd)]
    result = subprocess.run(command, input=ledger.canonical(selected), stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, user=value['runtime_uid'], group=value['runtime_gid'],
        extra_groups=[], env=host.environment(), pass_fds=(lease_fd, instance_fd, *cold_custody.inherited()), timeout=5)
    ledger.require(result.returncode == 0 and 0 < len(result.stdout) <= 16384,
                   'cold original intent verification failed')
    checked = store.decode(result.stdout)
    ledger.keys(checked, ('verified', 'request_sha256', 'core_sha256', 'resource',
                          'lease_path', 'model_actions'))
    ledger.require(checked['verified'] is True and checked['model_actions'] is False and
        checked['request_sha256'] == ledger.digest(selected), 'cold verifier receipt differs')
    return checked


def issue(directory, selected, lease_fd, instance_fd, peer, pidfd):
    device_store.privileged(); cold_requests.request(selected)
    ledger.require(lease_fd != instance_fd, 'cold lease and instance descriptors collide')
    with host.exclusive(directory) as directory:
        value, root, device_directory, expected_config = selection(directory)
        ledger.require(value['installation_id'] == selected['host_id'] and
            value['service_id'] == selected['service_id'] and
            value['artifact']['manifest_sha256'] == selected['manifest_sha256'],
            'cold request host or selected package differs')
        checked = verify_runtime(directory, value, root, selected, lease_fd, instance_fd)
        child.verify_lease(instance_fd, str(Path(value['state'])/'guardian.lock'))
        instance = os.fstat(instance_fd)
        ledger.require(stat.S_ISREG(instance.st_mode) and instance.st_uid == value['runtime_uid'] and
            stat.S_IMODE(instance.st_mode) == 0o600, 'cold instance descriptor ownership differs')
        held = domain.lease(lease_fd, checked['lease_path'], expected_config)
        ledger.require((held.st_dev, held.st_ino) == (selected['lease_device'], selected['lease_inode']),
                       'cold request actual lease differs')
        current = resources.read_public(expected_config['resource']['directory'])
        ledger.require(checked['resource'] == dict(directory=expected_config['resource']['directory'],
            publication=current), 'cold request resource epoch changed')
        with kernel.Scope(current['record']) as original:
            ledger.require(original.empty(), 'cold original service leaf is populated')
            peer_identity(peer, pidfd, selected, value, current['record'])
        previous = device_resource.lifecycle(device_directory, expected_config)
        if previous['phase'] == 'armed' and previous['boot_id'] != store.stamp().boot_id:
            # Continue only a previously armed installation. Manual disarm,
            # lost history and same-boot rule loss never imply automatic arm.
            # arm() checks this condition again under the original admin lock.
            domain.arm(device_directory, lease_fd, checked['lease_path'], renew_boot=True)
        with domain.managed(device_directory) as (device_directory, config, binding, executable):
            ledger.require(config == expected_config, 'cold device selection changed')
            with domain.scope(config) as (group, pin, target, allowed):
                ledger.require(resources.read_public(config['resource']['directory']) == current,
                               'cold request resource epoch changed after verification')
                peer_identity(peer, pidfd, selected, value, current['record'])
                record = domain.current(device_directory, config, binding, domain.Observation(lease_fd))
                control = device_control.Control(executable, lease_fd, checked['lease_path'])
                args = dict(target_fd=target, allowed_fd=allowed, pin=str(pin),
                            devices=record['devices'], identity=record['filter'])
                with control.call('adopt', args, inherited=(target, allowed)) as retained:
                    spent = cold_requests.spend(device_directory, config, selected)
                    charge = device_store.charge_once(binding, selected['manifest_sha256'],
                                                      held.st_dev, held.st_ino)
                    policy = charge['state']; ticket = policy['last_ticket']
                    claims = dict(service_id=config['service_id'], domain_id=config['domain_id'],
                        boot_id=ticket['boot_id'], manifest_sha256=selected['manifest_sha256'],
                        qualification_sha256=ticket['qualification_sha256'],
                        journal_receipt_sha256=charge['receipt_sha256'], ticket_revision=policy['revision'],
                        issued_ns=ticket['issued_ns'], expires_ns=ticket['expires_ns'],
                        runtime_uid=config['runtime_uid'], lease_device=held.st_dev,
                        lease_inode=held.st_ino, filter=retained.result)
                    with control.call('issue', dict(link_fd=retained.fds[0], program_fd=retained.fds[1],
                        admission=claims), inherited=retained.fds) as issued:
                        domain.empty(group)
                        peer_identity(peer, pidfd, selected, value, current['record'])
                        fds = []
                        try:
                            for fd in (*issued.fds, *retained.fds): fds.append(os.dup(fd))
                            result = dict(spent, device_ticket_revision=policy['revision'],
                                          model_actions=False)
                            return device_control.Reply(result, fds)
                        except BaseException:
                            for fd in fds: os.close(fd)
                            raise

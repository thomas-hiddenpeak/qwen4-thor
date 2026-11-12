"""Outer-side cold admission selection and one-attempt request construction."""
from copy import deepcopy
import os
from pathlib import Path

from tools.serve_recovery import cold_channel, host_install, ledger

ENVIRONMENT = 'Q4T_COLD_HOST_DIRECTORY'


class Host:
    def __init__(self, directory, state, digest, service_id, resource):
        self.directory = Path(directory).absolute()
        self.state, self.digest, self.service_id = str(Path(state).absolute()), digest, service_id
        self.resource = resource
        self.value = host_install.public(self.directory)
        self.check()

    def check(self):
        value = host_install.public(self.directory)
        ledger.require(value['phase'] == 'ready' and value['enabled'] and
            value['installation_id'] == self.value['installation_id'] and
            value['state'] == self.state and value['service_id'] == self.service_id and
            value['artifact'] == self.value['artifact'] and
            value['artifact']['manifest_sha256'] == self.digest and
            value['runtime_uid'] == os.geteuid() != 0 and value['runtime_gid'] == os.getegid(),
            'cold outer host binding is disabled or changed')
        ledger.require(self.resource is not None, 'cold outer requires the original resource capability')
        resource = self.resource.binding()
        ledger.require(resource['directory'] == value['resource']['directory'] and
            resource['publication']['domain_id'] == value['resource']['domain_id'] and
            resource['publication']['record']['policy'] == value['resource']['policy'],
            'cold outer resource binding differs')
        return value

    def available(self):
        """An auxiliary precheck saves attempts; issuance rechecks all authority."""
        value = self.check()
        with cold_channel.connect(value['installation_id'], timeout=.2) as sock:
            cold_channel.send(sock, dict(version=1, operation='status'))
            with cold_channel.receive(sock, count=0) as reply:
                answer = reply.result
                ledger.require(answer.get('ok') is True and
                    answer['result']['host_id'] == value['installation_id'] and
                    answer['result']['service_id'] == self.service_id,
                    'cold issuer status identity differs')
                status = answer['result']
                return status.get('accepting') is True and status.get('worker_pending') is False and \
                    status.get('admission_state') in ('armed', 'requalify_required')

    def request(self, attempt, lease_fd, instance_fd, *, tick):
        value = self.check(); held = os.fstat(lease_fd)
        selected = dict(version=1, host_id=value['installation_id'], service_id=self.service_id,
            manifest_sha256=self.digest, ticket=attempt['ticket'], serial=attempt['serial'],
            boot_id=attempt['boot_id'], custody=deepcopy(attempt['custody']),
            lease_device=held.st_dev, lease_inode=held.st_ino)
        return cold_channel.request(value['installation_id'], selected, lease_fd, instance_fd, tick=tick)


def inherited(state, digest, service_id, resource):
    raw = os.environ.pop(ENVIRONMENT, None)
    if raw is None: return None
    ledger.require(raw and Path(raw).is_absolute(), 'absolute cold host installation required')
    return Host(raw, state, digest, service_id, resource)

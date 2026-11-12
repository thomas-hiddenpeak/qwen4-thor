"""Adopt only observation of a source-bound surviving managed Guardian.

No model signal, launch, budget mutation or tree-cleanup authority is exposed.
A lost control reply is UNKNOWN; only the pinned kernel pidfd proves root exit,
and even that does not prove the rest of its service tree empty.
"""
from contextlib import ExitStack
import os
from pathlib import Path
import select

from tools.serve_host import native
from tools.serve_recovery import access_store, bundle, control, deployment, journal
from tools.serve_recovery import ledger, lifecycle, outer_store, store


class Observer:
    def __init__(self, state, package, digest):
        self.fd = -1
        self.locks = ExitStack()
        try:
            self.state = Path(state).absolute()
            manifest = bundle.verify(package, digest)
            ledger.require(manifest['version'] >= 18, 'outer observation requires profile v18')
            self.locks.enter_context(deployment.claim(self.state, shared=True))
            self.desc = lifecycle.selected_state(self.state, package, digest)
            self.binding = access_store.binding(self.state, self.desc['service_id'])
            self.boot_id = store.stamp().boot_id
            policy = lifecycle.read_family(self.state, self.desc, outer_store.Store)
            self.attempt = policy['pending']
            ledger.require(self.attempt is not None and self.attempt['boot_id'] == self.boot_id and
                self.attempt['manifest_sha256'] == digest and self.attempt['cleanup'] is None,
                'no current unresolved managed outer attempt')
            self.executable = native.inode(Path(package)/'bin/q4t-guardian')
            self.context = {name:native.inode('/proc/self/ns/'+name)
                            for name in ('pid', 'user', 'net', 'mnt')}
            self.owner = dict(pid=os.getpid(), start_ticks=journal.process_start(os.getpid()),
                              boot_id=self.boot_id)
            first = control.request(self.desc['service_id'], 'status', timeout=.5)
            self.root = dict(pid=first['guardian_pid'], start_ticks=first['guardian_start_ticks'],
                             boot_id=self.boot_id)
            ledger.owner_valid(self.root)
            self.fd = os.pidfd_open(self.root['pid'])
            self.poller = select.poll(); self.poller.register(self.fd, select.POLLIN)
            self.verify(first)
            ledger.require(not self.exited(), 'managed Guardian exited during observation adoption')
        except BaseException:
            self.close()
            raise

    def verify(self, value):
        ledger.require(access_store.binding(self.state, self.desc['service_id']) == self.binding and
            deployment.read_descriptor(self.state) == self.desc and
            store.stamp().boot_id == self.boot_id, 'outer observation deployment changed')
        ledger.require(value['guardian_pid'] == self.root['pid'] and
            value['guardian_start_ticks'] == self.root['start_ticks'] and
            value.get('outer_supervised') is True, 'outer observation root changed')
        origin = self.attempt['custody']['supervisor']
        ledger.require(value.get('outer_origin_pid') == origin['pid'] and
            value.get('outer_origin_start_ticks') == origin['start_ticks'] and
            self.attempt['owner'] in (None, self.root), 'outer observation origin changed')
        native.process(self.root['pid'], self.root['start_ticks'], self.executable, self.context)
        return value

    def exited(self):
        ledger.require(self.fd >= 0, 'outer observation capability is closed')
        events = self.poller.poll(0)
        ledger.require(all(fd == self.fd and not mask & (select.POLLNVAL | select.POLLERR)
                           for fd, mask in events),
                       'invalid outer process capability')
        return any(mask & (select.POLLIN | select.POLLHUP) for _, mask in events)

    def sample(self, *, adopt=False):
        if self.exited():
            return dict(state='EXITED', root_exited=True, tree_cleanup_verified=False,
                        model_actions=False, device_authorized=False)
        try:
            value = self.verify(control.outer_observation(self.desc['service_id'], adopt=adopt))
            ledger.require(value.get('outer_observation_accepted') is True and
                value.get('outer_owner_pid') == self.owner['pid'] and
                value.get('outer_owner_start_ticks') == self.owner['start_ticks'] and
                value.get('outer_owner_lost') is False, 'outer observation was not adopted')
            ledger.require(not self.exited(), 'Guardian exited after observation reply')
            return dict(state='ATTACHED', root_exited=False, guardian=value,
                        tree_cleanup_verified=False, model_actions=False, device_authorized=False)
        except (OSError, ValueError, KeyError, TypeError):
            return dict(state='UNKNOWN', root_exited=self.exited(), tree_cleanup_verified=False,
                        model_actions=False, device_authorized=False)

    def close(self):
        if self.fd >= 0:
            os.close(self.fd);self.fd = -1
        self.locks.close()

    def __enter__(self):return self
    def __exit__(self, *_args):self.close()

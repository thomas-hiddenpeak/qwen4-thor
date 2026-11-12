"""Asynchronous profile intentions; lifecycle authority remains in Guardian."""
import copy
import time
import uuid

from tools.serve_recovery import ledger, profile_contract, profile_io, profile_policy


class Selection:
    def __init__(self, binding, registry, boot_id, *, configuration=None, clock=time.monotonic,
                 nanos=time.monotonic_ns):
        profile_contract.validate(registry)
        ledger.Stamp(boot_id, 0)
        self.registry = copy.deepcopy(registry)
        self.configuration = profile_policy.configuration(
            profile_policy.DEFAULTS if configuration is None else configuration)
        self.boot_id, self.nanos = boot_id, nanos
        self.worker = profile_io.Worker(binding, clock=clock)
        self.state = None
        self.operation = 'load'
        self.resumed_worker = 0
        self.resume_worker = 0
        self.parked = False
        self.error = 'profile_state_wait'

    def stamp(self):
        return ledger.Stamp(self.boot_id, self.nanos())

    def check(self, state):
        profile_policy.validate(state)
        ledger.require(state['registry_sha256'] == profile_contract.digest(self.registry) and
            state['profiles'] == {n: ledger.digest(v) for n, v in self.registry['profiles'].items()} and
            state['configuration'] == self.configuration,
            'profile registry binding changed')

    def step(self, *, allow_start=True):
        try:
            self.worker.step(allow_start=allow_start and not self.parked)
            if self.parked: return
            if not self.worker.applied():
                self.error = self.worker.error or 'profile_commit_wait'
                return
            self.check(self.worker.value)
            self.state = copy.deepcopy(self.worker.value)
            if self.operation == 'resume' and self.resume_worker == self.worker.started:
                self.resumed_worker = self.worker.started
            if self.resumed_worker != self.worker.started:
                candidate = profile_policy.resume(self.state, self.stamp())
                self.worker.commit(self.state, candidate)
                self.resume_worker = self.worker.started
                self.operation = 'resume'
                self.error = 'profile_resume_wait'
                return
            self.operation = 'idle'
            self.worker.next_read = float('inf')
            self.error = ''
        except Exception:
            # A bad reader or journal cannot signal a healthy model. Only the
            # controller's independently established core faults may do so.
            self.error = 'profile_state_unavailable'

    def ready(self):
        return (not self.parked and not self.error and self.operation == 'idle' and
                self.state is not None and self.worker.applied() and
                self.resumed_worker == self.worker.started)

    def prepare(self, target, reason, generation):
        if not self.ready(): return False
        stamp = self.stamp()
        check = profile_policy.permission(self.state, target, stamp)
        if not check['allowed']:
            return False
        request_id = str(uuid.uuid4())
        candidate = profile_policy.prepare(self.state, target, reason, request_id,
                                           generation, stamp)
        self.worker.commit(self.state, candidate, request_id)
        self.operation = 'prepare'
        self.error = 'profile_commit_wait'
        return True

    def confirm(self, generation, name):
        if not self.ready() or self.state['pending'] is None: return False
        ledger.require(name in self.registry['profiles'], 'unknown active profile')
        candidate = profile_policy.finish(self.state, generation,
            ledger.digest(self.registry['profiles'][name]), self.stamp())
        self.worker.commit(self.state, candidate)
        self.operation = 'finish'
        self.error = 'profile_commit_wait'
        return True

    def park(self):
        """Reap the I/O domain before Guardian's empty-tree launch proof."""
        self.parked = True
        self.worker.disable()
        self.step(allow_start=False)
        return self.worker.process is None and self.worker.claim is None

    def unpark(self):
        if not self.parked: return
        # Reload/rebase after an owned cleanup; never trust a pre-cleanup
        # unknown commit. The journal reconciles the exact persisted intent.
        self.parked = False
        self.worker.reload()
        self.operation = 'load'
        self.error = 'profile_state_wait'

    def status(self):
        return dict(ready=self.ready(), error=self.error, parked=self.parked,
            selection=None if self.state is None else profile_policy.describe(self.state),
            worker=self.worker.status(), model_actions=False)

    def close(self):
        self.parked = True
        return self.worker.close()

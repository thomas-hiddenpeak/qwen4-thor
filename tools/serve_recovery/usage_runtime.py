"""Known/unknown quota recovery and bounded clean handoff, without core signals."""
import copy
import time

from tools.serve_recovery import ledger, quota_control, usage_control, usage_io, usage_policy


class Recovery:
    def __init__(self, binding, *, clock=time.monotonic, nanos=time.monotonic_ns):
        self.clock, self.nanos = clock, nanos
        self.worker = usage_io.Worker(binding, clock=clock)
        self.service_id = binding['service_id']
        self.channel = self.owner = None
        self.generation = 0
        self.restored = self.sealed = False
        self.error = ''

    def detach(self):
        if self.channel is not None:self.channel.close()
        self.channel = self.owner = None
        self.worker.disable()
        self.restored = self.sealed = False

    def attach(self, owner, generation, not_before_ns):
        self.detach()
        ledger.owner_valid(owner)
        ledger.require(ledger.integer(generation, minimum=1) and ledger.integer(not_before_ns),
                       'invalid usage recovery identity')
        self.owner, self.generation = dict(owner), generation
        self.channel = usage_control.Channel(quota_control.address(self.service_id, generation),
                                             clock=self.clock)
        self.worker.begin(generation, owner)
        self.after = (self.worker.started, self.worker.sequence)
        self.not_before_ns = not_before_ns
        self.prepare_until = self.clock()+5
        self.prepared = self.closing = self.seal_sent = False
        self.mode = 'waiting'
        self.payload = self.frozen = None
        self.restore_started = None
        self.receipt = ''
        self.error = 'client_usage_wait'

    def freeze(self):
        if self.owner is None or not self.restored:return False
        self.closing = True
        self.channel.request('usage_freeze')
        return True

    def step(self, *, allow_start=True):
        try:
            self.worker.step(allow_start=allow_start and self.owner is not None)
            if self.owner is None:return
            if self.closing:
                self._seal()
                return
            if self.worker.applied():self.worker.next_read = float('inf')
            if self.restored:return  # Auxiliary loss cannot undo live quotas.
            if not self.prepared:
                after = (self.worker.started, self.worker.completed_sequence) > self.after
                if self.worker.applied() and after:
                    plan = usage_policy.restoration(self.worker.value, self.owner['boot_id'], self.nanos())
                    self.mode, self.payload = plan['mode'], plan['snapshot']
                    self.prepared = True
                elif self.clock() >= self.prepare_until:
                    self.mode, self.payload, self.prepared = 'unknown', None, True
                else:return
            if self.mode != 'exact' and self.nanos() < self.not_before_ns:
                self.error = 'client_usage_conservative_window';return
            if self.restore_started is None:
                self.channel.request('usage_restore', self.payload)
                self.restore_started = self.clock()
            result = self.channel.step(self.owner['pid'], self.owner['start_ticks'])
            if result['applied']:
                self.restored = True;self.receipt = result['receipt_sha256'];self.error = ''
            elif (self.payload is not None and self.clock()-self.restore_started >= 3 and
                  self.channel.phase is None and result['observed'] is not None and
                  not result['observed']['recovered'] and
                  result['error'] == 'client_usage_restore_refused'):
                # Only an explicit, completed refusal permits abandoning this
                # snapshot. An unanswered restore may already have applied.
                self.mode, self.payload, self.restore_started = 'unknown', None, None
                self.error = 'client_usage_snapshot_refused'
            else:self.error = result['error'] or 'client_usage_wait'
        except Exception:
            self.error = 'client_usage_auxiliary_unavailable'

    def _seal(self):
        if self.frozen is None:
            result = self.channel.step(self.owner['pid'], self.owner['start_ticks'])
            if not result['applied']:
                self.error = result['error'] or 'client_usage_drain_wait';return
            self.frozen = copy.deepcopy(result['observed']['snapshot'])
        if not self.seal_sent:
            self.worker.seal(self.generation, self.owner, self.frozen)
            self.seal_sent = True
        if self.worker.applied():
            self.sealed = True;self.worker.next_read = float('inf');self.error = ''
        else:self.error = self.worker.error or 'client_usage_persistence_wait'

    def close(self):
        self.detach()
        return self.worker.close()

    def status(self):
        return dict(generation=self.generation, restored=self.restored, sealed=self.sealed,
            mode=getattr(self, 'mode', 'detached'), error=self.error,
            receipt_sha256=getattr(self, 'receipt', ''), worker=self.worker.status(),
            model_actions=False)

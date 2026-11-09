"""Controller-owned policy coordination. No model signals or recovery decisions."""
from tools.serve_recovery import access_io, ledger, quota_control


class Policies:
    def __init__(self, binding):
        ledger.keys(binding, ('state_directory', 'service_id', 'state_identity'))
        self.reader = access_io.Reader(binding['state_directory'], binding['service_id'],
                                      binding['state_identity'], kind='limits')
        self.service_id = binding['service_id']
        self.channel = None
        self.owner = None
        self.token = None
        self.after = (0, 0)
        self.installed = False
        self.error = 'client_policy_unavailable'

    def detach(self):
        if self.channel is not None:self.channel.close()
        self.channel = self.owner = self.token = None
        self.installed = False

    def attach(self, owner, generation, token):
        self.detach()
        ledger.require(type(owner) is dict and ledger.integer(owner.get('pid'), minimum=2) and
            ledger.integer(owner.get('start_ticks'), minimum=1), 'invalid policy model owner')
        # A new model cannot reuse a pre-generation cached read after storage
        # fails. Require a completed load that started after attachment.
        self.after = (self.reader.started, self.reader.sequence)
        self.reader.next_read = 0
        self.owner = dict(owner)
        self.token = token
        self.channel = quota_control.Channel(quota_control.address(self.service_id, generation))
        self.error = 'client_policy_wait'

    def step(self, *, allow_start=True):
        try:
            self.reader.step(allow_start=allow_start)
            if self.channel is None:return
            read = (self.reader.started, self.reader.completed_sequence)
            if self.reader.available and read > self.after:
                self.channel.desired(self.reader.value, self.token)
            elif self.channel.wanted is None:
                self.reader.next_read = 0
            observed = self.channel.step(self.owner['pid'], self.owner['start_ticks'])
            if observed['live_applied']:self.installed = True
            self.error = self.reader.error or observed['error']
        except Exception:
            # A policy helper failure is not evidence of model failure. A
            # previously installed snapshot and its in-memory quotas survive.
            self.error = 'client_policy_auxiliary_unavailable'

    def close(self):
        self.detach()
        return self.reader.close()

    def status(self):
        return dict(installed=self.installed, error=self.error, reader=self.reader.status(),
            publication=None if self.channel is None else self.channel.status(), model_actions=False)

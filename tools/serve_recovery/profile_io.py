"""One bounded profile journal slot, with exact durable transaction retries."""
import copy
import uuid

from tools.serve_recovery import access_io, ledger, profile_policy


class Worker(access_io.Reader):
    def __init__(self, binding, **options):
        ledger.keys(binding, ('state_directory', 'service_id', 'state_identity'))
        super().__init__(binding['state_directory'], binding['service_id'],
                         binding['state_identity'], kind='profiles', **options)
        self.wanted = dict(op='load')

    def commit(self, previous, candidate, request_id=None):
        profile_policy.transition(previous, candidate)
        request_id = request_id or str(uuid.uuid4())
        ledger.require(ledger.identifier(request_id), 'invalid profile request')
        self.wanted = copy.deepcopy(dict(op='commit', request_id=request_id,
            expected_revision=previous['revision'], candidate=candidate))
        self.next_read = 0

    def reload(self):
        self.wanted = dict(op='load')
        self.next_read = 0

    def command(self):
        return self.wanted

    def applied(self):
        return (self.available and self.value is not None and
                (self.wanted['op'] == 'load' or
                 self.value == self.wanted['candidate']))

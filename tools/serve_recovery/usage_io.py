"""Nonblocking usage transactions using the independently owned I/O slot."""
import copy

from tools.serve_recovery import access_io, ledger, usage_policy


class Worker(access_io.Reader):
    def __init__(self, binding, **options):
        ledger.keys(binding, ('state_directory', 'service_id', 'state_identity'))
        super().__init__(binding['state_directory'], binding['service_id'],
                         binding['state_identity'], kind='usage', **options)
        self.wanted = dict(op='load')

    def begin(self, generation, owner):
        ledger.owner_valid(owner)
        ledger.require(ledger.integer(generation, minimum=1), 'invalid usage generation')
        self.wanted = copy.deepcopy(dict(op='begin', generation=generation, owner=owner))
        self.next_read = 0

    def seal(self, generation, owner, snapshot):
        ledger.owner_valid(owner);usage_policy.snapshot(snapshot)
        ledger.require(ledger.integer(generation, minimum=1), 'invalid usage generation')
        self.wanted = copy.deepcopy(dict(op='seal', generation=generation,
                                        owner=owner, snapshot=snapshot))
        self.next_read = 0

    def command(self):
        return self.wanted

    def applied(self):
        if not self.available or self.value is None:return False
        if self.wanted['op'] == 'load':return True
        return (self.value['generation'] == self.wanted['generation'] and
                self.value['owner'] == self.wanted['owner'] and
                self.value['phase'] == ('active' if self.wanted['op'] == 'begin' else 'sealed') and
                (self.wanted['op'] != 'seal' or self.value['snapshot'] == self.wanted['snapshot']))

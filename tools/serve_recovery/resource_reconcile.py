"""New-instance retirement using the original, kernel-enforced service domain.

This is not a process scan, fresh empty group, or monitoring-error response.
Actual exclusive instance custody excludes the old Guardian, and the durable
pre-fork attempt binds the selected root-owned original domain. Only that leaf
can be killed; outer observation and unrelated services are outside it.
"""
from copy import deepcopy
import uuid

from tools.serve_recovery import deployment, ledger, outer_instance, outer_policy
from tools.serve_recovery import outer_reconcile, outer_store, resource_launch, store


def retire(instance, capability):
    ledger.require(type(instance) is outer_instance.Instance and
        type(capability) is resource_launch.Capability, 'actual instance and resource custody required')
    instance.check(); selected = capability.binding()
    state = instance.path.parent
    with deployment.claim(state, shared=True):
        desc = deployment.read_descriptor(state); deployment.ready(state, desc)
        ledger.require(desc['service_id'] == instance.binding['service_id'] ==
            selected['publication']['service_id'], 'resource deployment identity changed')
        with outer_store.open_bound(instance.binding) as outer:
            previous = outer.head['state']['policy']; pending = previous['pending']
            if pending is not None:
                ledger.require(pending.get('resource') == selected and
                    pending['manifest_sha256'] == desc['manifest_sha256'] and
                    pending['boot_id'] == store.stamp().boot_id and pending['cleanup'] is None,
                    'original resource attempt does not match actual domain')
                instance.check(); capability.check()
                # No active Guardian can coexist with this exclusive instance.
                # A surviving journal may hold the core writer until its domain
                # exits. Domain ownership is proven BEFORE acquiring that lock.
                capability.scope.kill_service()
            instance.check(); capability.check()
            ledger.require(capability.scope.empty(), 'original service domain is not empty')
            with store.Store(state, desc['service_id']) as core:
                ledger.require(core.service_contract, 'resource retirement requires service state fence')
                if pending is None:
                    ledger.require(not any(core.state['active'].values()),
                                   'resource attempt missing while core slots remain')
                    return dict(reconciled=True, already_retired=True, device_authorized=False,
                                model_starts=0, resource_cleanup=True)
                outer_reconcile.core_successor(core.state, pending)
                before = deepcopy(core.state); retired = []
                for role in ('model', 'controller', 'gateway'):
                    active = core.state['active'][role]
                    if active is None: continue
                    instance.check(); capability.check()
                    ledger.require(capability.scope.empty(), 'service domain repopulated during retirement')
                    ticket = active['ticket']
                    core.transact(dict(id=str(uuid.uuid4()), expected_revision=core.state['revision'],
                        operation='retired', args=dict(role=role, ticket=ticket, verified=True)))
                    retired.append(dict(role=role, ticket=ticket))
                ledger.require(not any(core.state['active'].values()) and all(
                    core.state[key] == before[key] for key in ('desired', 'quarantine', 'generations',
                        'attempts', 'failures', 'not_before', 'circuit_until')),
                    'resource retirement changed original core protection or budget')
                candidate = outer_policy.change(previous, 'retired',
                    dict(ticket=pending['ticket'], verified=True), store.stamp())
                outer.transact(str(uuid.uuid4()), previous['revision'], candidate)
                return dict(reconciled=True, already_retired=False, retired=retired,
                    device_authorized=False, model_starts=0, resource_cleanup=True)

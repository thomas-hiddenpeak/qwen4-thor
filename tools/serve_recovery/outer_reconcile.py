"""Retire only slots born under an actually reaped, continuously owned tree.

This path never starts a model, acknowledges a fault, or supplies device proof.
The original kernel tree owner and service instance lock must both survive;
another process cannot reconstruct that authority from a saved JSON witness.
"""
from copy import deepcopy
import uuid

from tools.serve_recovery import deployment, ledger, outer_policy, outer_store
from tools.serve_recovery import outer_tree, store


def core_successor(core, attempt):
    ledger.require(core['revision'] >= attempt['core_revision'] and all(
        core['generations'][role] >= attempt['core_generations'][role]
        for role in ledger.ROLES), 'core history regressed before outer cleanup')
    if core['revision'] == attempt['core_revision']:
        ledger.require(ledger.digest(core) == attempt['core_sha256'],
                       'original core state was replaced')
    for role, active in core['active'].items():
        if active is None: continue
        ledger.require(active['generation'] > attempt['core_generations'][role] and
            (active['owner'] is None or active['owner']['boot_id'] == attempt['boot_id']),
            'active slot does not belong to this outer attempt')


def continuous(instance, tree):
    ledger.require(type(tree) is outer_tree.Tree and tree.attempt is not None,
                   'actual continuous tree required for reconciliation')
    instance.check()
    attempt = tree.attempt
    witness = tree.continuous(instance, attempt)
    state = instance.path.parent
    with deployment.claim(state, shared=True):
        desc = deployment.read_descriptor(state)
        ledger.require(desc['service_id'] == instance.binding['service_id'] and
            desc['manifest_sha256'] == attempt['manifest_sha256'],
            'outer deployment identity changed')
        deployment.ready(state, desc)
        with outer_store.open_bound(instance.binding) as outer:
            previous = outer.head['state']['policy']
            pending = previous['pending']
            if pending is not None:
                tree.continuous(instance, pending)
            else:
                ledger.require(previous['total'] == attempt['serial'],
                               'another outer attempt has superseded cleanup')
            with store.Store(state, desc['service_id']) as core:
                ledger.require(core.service_contract, 'outer cleanup requires service state fence')
                core_successor(core.state, attempt)
                if pending is None:
                    ledger.require(not any(core.state['active'].values()),
                                   'retired outer attempt still has active slots')
                    return dict(reconciled=True, already_retired=True, witness=witness,
                                device_authorized=False, model_actions=False)
                before = deepcopy(core.state)
                retired = []
                # The core contract requires the model slot before controller.
                for role in ('model', 'controller', 'gateway'):
                    active = core.state['active'][role]
                    if active is None: continue
                    instance.check()
                    tree.continuous(instance, pending)
                    ticket = active['ticket']
                    core.transact(dict(id=str(uuid.uuid4()),
                        expected_revision=core.state['revision'], operation='retired',
                        args=dict(role=role, ticket=ticket, verified=True)))
                    retired.append(dict(role=role, ticket=ticket))
                ledger.require(not any(core.state['active'].values()) and all(
                    core.state[key] == before[key] for key in
                    ('desired', 'quarantine', 'generations', 'failures', 'not_before', 'circuit_until')),
                    'outer cleanup changed core protection or history')
                candidate = outer_policy.change(previous, 'retired',
                    dict(ticket=pending['ticket'], verified=True), store.stamp())
                outer.transact(str(uuid.uuid4()), previous['revision'], candidate)
                return dict(reconciled=True, already_retired=False, retired=retired,
                    core_revision=core.state['revision'], witness=witness,
                    device_authorized=False, model_actions=False)


def reboot_checkpoint(core, attempt, actual_boot):
    cleanup = attempt['cleanup']
    ledger.require(actual_boot != attempt['boot_id'] and cleanup is not None and
        core['revision'] >= cleanup['core_revision'] and
        core['generations'] == cleanup['core_generations'],
        'reboot cleanup checkpoint no longer matches core')
    for role, active in core['active'].items():
        if active is None: continue  # Already retired by this persistent intent.
        ledger.require({key:active[key] for key in ('ticket', 'generation', 'owner')} ==
            cleanup['slots'][role], 'reboot cleanup slot was replaced')


def reboot(instance, *, resource=None):
    """Actual new host boot proves prior-boot tasks gone, never GPU readiness.

    Persist exact old tickets BEFORE the first retirement; subsequent attempts
    can finish partial cleanup even after stop updated the core's boot clock.
    Current-boot or unknown-birth grants cannot be swept into that old intent.
    """
    instance.check()
    now = store.stamp()
    state = instance.path.parent
    with deployment.claim(state, shared=True):
        desc = deployment.read_descriptor(state)
        ledger.require(desc['service_id'] == instance.binding['service_id'], 'foreign reboot state')
        deployment.ready(state, desc)
        with outer_store.open_bound(instance.binding) as outer:
            previous = outer.head['state']['policy']
            pending = previous['pending']
            if pending is None:
                with store.Store(state, desc['service_id']) as core:
                    ledger.require(core.service_contract and not any(core.state['active'].values()),
                                   'unbound reboot slots require actual reconciliation')
                return dict(reconciled=True, mode='no_pending', device_authorized=False,
                            model_actions=False)
            ledger.require(pending is not None and pending['boot_id'] != now.boot_id and
                pending['manifest_sha256'] == desc['manifest_sha256'],
                'no matching previous-boot outer attempt')
            selected = None
            def verify_resource():
                nonlocal selected
                if 'resource' not in pending: return
                from tools.serve_recovery import resource_launch
                ledger.require(type(resource) is resource_launch.Capability,
                               'original resource entry is required for reboot reconciliation')
                selected = resource.binding()
                resource_launch.successor(pending['resource'], selected, store.stamp().boot_id)
                ledger.require(resource.scope.empty(), 'new resource epoch is populated')
            verify_resource()
            with store.Store(state, desc['service_id']) as core:
                ledger.require(core.service_contract, 'reboot cleanup requires service state fence')
                if pending['cleanup'] is None or pending['cleanup']['boot_id'] != now.boot_id:
                    args = dict(ticket=pending['ticket'], core=core.state)
                    if selected is not None: args['resource'] = selected
                    candidate = outer_policy.change(previous,
                        'prepare_reboot' if pending['cleanup'] is None else 'advance_reboot', args, now)
                    outer.transact(str(uuid.uuid4()), previous['revision'], candidate)
                    previous = candidate; pending = candidate['pending']
                reboot_checkpoint(core.state, pending, now.boot_id)
                before = deepcopy(core.state); retired = []
                for role in ('model', 'controller', 'gateway'):
                    active = core.state['active'][role]
                    if active is None: continue
                    instance.check()
                    verify_resource()
                    reboot_checkpoint(core.state, pending, store.stamp().boot_id)
                    ticket = active['ticket']
                    core.transact(dict(id=str(uuid.uuid4()), expected_revision=core.state['revision'],
                        operation='retired', args=dict(role=role, ticket=ticket, verified=True)))
                    retired.append(dict(role=role, ticket=ticket))
                ledger.require(not any(core.state['active'].values()) and all(
                    core.state[key] == before[key] for key in
                    ('desired', 'quarantine', 'generations', 'failures', 'not_before', 'circuit_until')),
                    'reboot cleanup changed core protection or history')
                candidate = outer_policy.change(previous, 'retired',
                    dict(ticket=pending['ticket'], verified=True), store.stamp())
                outer.transact(str(uuid.uuid4()), previous['revision'], candidate)
                return dict(reconciled=True, mode='previous_boot', retired=retired,
                    original_boot_id=pending['boot_id'], observed_boot_id=now.boot_id,
                    core_revision=core.state['revision'], device_authorized=False, model_actions=False)

"""Root-private payload preparation, after independent whole-tree retirement.

The version worker owns no role grants and sends no model signals. The native
root alone may call prepare after it has closed admission, reaped every old
payload descendant and durably retired the core role slots. The original
profile journal is migrated in place; keys, quotas, objects and core budgets
are never replaced. A lost reply retries the same version switch identity.
"""
import copy
from pathlib import Path
import uuid

from tools.serve_recovery import access_store, deployment, ledger, profile_manager
from tools.serve_recovery import profile_migration, profile_store, version_contract
from tools.serve_recovery import version_policy, version_registration


def prepare(binding, selected, args):
    ledger.keys(args, ('root_manifest_sha256', 'payload', 'cleanup_complete',
                       'lease_continuous', 'roles_retired'))
    ledger.require(all(args[k] is True for k in
        ('cleanup_complete', 'lease_continuous', 'roles_retired')),
        'payload preparation needs native cleanup and retirement proof')
    version_policy.validate(selected)
    ledger.require(selected['terminal_fault'] is None and
        args['payload'] == version_policy.launch_payload(selected) and
        args['root_manifest_sha256'] == selected['registry']['root_manifest_sha256'],
        'payload preparation selection differs or recovery is exhausted')
    state = Path(binding['state_directory'])
    root = deployment.read_descriptor(state)
    ledger.require(root['service_id'] == binding['service_id'], 'version service changed')
    registration = version_registration.for_state(state,root,selected)['registration']
    first,second = [version_contract.payload(registration['payloads'][name]['descriptor'])
                    for name in version_policy.PAYLOADS]
    target = first if args['payload'] == 'primary' else second
    authorization = version_contract.compatible(first,second)
    # This deterministic ID belongs to the durable switch, not the worker
    # session or its receipt. Retry cannot replenish profile clocks/budgets.
    request = (None if selected['switch'] is None else str(uuid.uuid5(
        uuid.UUID(selected['switch']['request_id']), 'version-profile-migration')))

    class AuthorizedStore(profile_store.Store):
        contract = profile_migration.AuthorizedContract(authorization)

        def reconcile(self):
            if self.exists('policy.pending') and self.exists('policy.json'):
                before = self.envelope('policy.json')['state']['policy']
                pending = self.envelope('policy.pending')['state']
                if before['registry_sha256'] != pending['policy']['registry_sha256']:
                    ledger.require(request is not None and
                        pending['receipts'][-1]['id'] == request,
                        'pending profile migration is not this version switch')
            super().reconcile()

    with access_store.open_bound(binding,AuthorizedStore) as writer:
        current = writer.head['state']['policy']
        wanted = target['binding']['profile_registry_sha256']
        if current['registry_sha256'] != wanted:
            ledger.require(args['payload'] == 'fallback' and request is not None,
                           'cannot reset profile selection to primary')
            profile_manager.matches(current,first['manifest'])
            clock = current['clock']
            candidate = profile_migration.candidate(current,authorization,
                ledger.Stamp(clock['boot_id'],clock['monotonic_ns']))
            writer.transact(request,current['revision'],candidate)
        profile_manager.matches(writer.head['state']['policy'],target['manifest'])
        value = copy.deepcopy(writer.head['state']['policy'])
    return dict(payload=args['payload'], manifest_sha256=target['descriptor']['manifest_sha256'],
        binding_sha256=ledger.digest(target['binding']),
        profile_registry_sha256=value['registry_sha256'], profile_revision=value['revision'])

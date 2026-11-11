"""Explicit STOPPED profile bootstrap under existing deployment/core locks."""
from pathlib import Path
import uuid

from tools.serve_recovery import access_store, ledger, profile_contract
from tools.serve_recovery import deployment_policy, profile_migration
from tools.serve_recovery import profile_policy, profile_store
from tools.serve_service import package as engine


def matches(state, manifest):
    registry = manifest.get('recovery_profiles')
    ledger.require(registry is not None, 'package has no qualified profiles')
    profile_policy.validate(state)
    ledger.require(state['registry_sha256'] == profile_contract.digest(registry) and
        state['profiles'] == {n: ledger.digest(v) for n, v in registry['profiles'].items()} and
        state['configuration'] == manifest['recovery_profile_policy'],
        'profile journal differs from sealed qualification/policy')
    return state


def evidence(package, manifest):
    """Read exact observations from an already verified sealed package."""
    result = {}
    for name in profile_contract.NAMES:
        filename = 'profile-'+name+'.json'
        path = Path(package)/filename
        digest = engine.sha(path)
        ledger.require(digest == manifest['files'][filename],
                       'migration qualification file changed')
        result[name] = dict(record=engine.read(path), sha256=digest)
    return result


def compatible(old, new, old_package=None, new_package=None):
    if old.get('recovery_profiles') is None:
        return None
    ledger.require(new.get('recovery_profiles') is not None,
                   'deployment would discard qualified profile state')
    if (old['recovery_profiles'] == new['recovery_profiles'] and
            old['recovery_profile_policy'] == new['recovery_profile_policy']):
        return None
    ledger.require(old_package is not None and new_package is not None,
                   'migration requires both sealed qualification packages')
    return profile_migration.permit(old['recovery_profiles'],
        new['recovery_profiles'], evidence(old_package, old),
        evidence(new_package, new), old['recovery_profile_policy'],
        new['recovery_profile_policy'], allow_capacity_change=True)


def preflight(binding, old, new):
    """Before publishing an intent, verify the target can read current history."""
    if old.get('recovery_profiles') is None:
        return
    with profile_store.open_bound(binding) as writer:
        current = matches(writer.head['state']['policy'], old)
        historical = current['format'] == profile_policy.HISTORY_FORMAT
    capacity_change = old['recovery_profiles']['profiles'] != new['recovery_profiles']['profiles']
    if historical or capacity_change:
        # Older control payloads can retain all original logic with only the
        # qualified representation adapter; do not rewrite their history.
        name = 'source/tools/serve_recovery/profile_policy.py'
        ledger.require(new['files'].get(name) == engine.sha(Path(profile_policy.__file__)),
                       'target needs the qualified profile history adapter')


def deploy(binding, intent, old, new, core):
    """Apply only the locked, durable STOPPED deployment's qualified change.

    No wall time enters this representation transition. The next runtime
    session rebases its own clock without giving downtime budget credit.
    Thus a retry can reproduce the exact candidate from its predecessor, or
    replay its last receipt, without an extra mutable migration sidecar.
    """
    deployment_policy.validate(intent)
    ledger.require(intent['phase'] == 'prepared' and
        intent['current']['service_id'] == binding['service_id'] and
        core['desired'] == 'STOPPED' and not any(core['active'].values()),
        'profile migration requires stopped prepared deployment')
    authorization = compatible(old, new, intent['current']['package'],
                               intent['target']['package'])
    if authorization is None:
        return initialize(binding, new, core,
                          previously_enabled=old.get('recovery_profiles') is not None)
    request = str(uuid.uuid5(uuid.UUID(intent['request']), 'profile-migration'))

    class AuthorizedStore(profile_store.Store):
        contract = profile_migration.AuthorizedContract(authorization)

        def reconcile(self):
            if self.exists('policy.pending') and self.exists('policy.json'):
                previous = self.envelope('policy.json')['state']['policy']
                pending = self.envelope('policy.pending')['state']
                following = pending['policy']
                if any(previous[k] != following[k]
                       for k in ('registry_sha256', 'configuration')):
                    ledger.require(pending['receipts'][-1]['id'] == request,
                                   'pending profile migration belongs to another deployment')
            super().reconcile()

    with access_store.open_bound(binding, AuthorizedStore) as writer:
        state = writer.head['state']
        current = state['policy']
        receipts = state['receipts']
        if any(r['id'] == request for r in receipts):
            ledger.require(receipts[-1]['id'] == request,
                           'profile state advanced during stopped deployment')
            matches(current, new)
            result = writer.transact(request, current['revision']-1, current)
        else:
            matches(current, old)
            clock = current['clock']
            candidate = profile_migration.candidate(current, authorization,
                ledger.Stamp(clock['boot_id'], clock['monotonic_ns']))
            result = writer.transact(request, current['revision'], candidate)
        matches(writer.head['state']['policy'], new)
        return result


def initialize(binding, manifest, core, *, previously_enabled):
    """Only a durable unpublished init/upgrade intent may create this family.

    Core/deployment/Guardian locks must already be held. A marker, even when
    its journal has disappeared, always forbids restoring initial selection.
    """
    if manifest.get('recovery_profiles') is None: return
    ledger.require(core['desired'] == 'STOPPED' and not any(core['active'].values()),
                   'profile initialization requires stopped authority')
    if not previously_enabled:
        clock = core['clock']
        initial = profile_policy.initial(manifest['recovery_profiles'],
            ledger.Stamp(clock['boot_id'], clock['monotonic_ns']),
            manifest['recovery_profile_policy'])
        access_store.bootstrap_bound(binding, initial, profile_store.Store)
    with profile_store.open_bound(binding) as writer:
        matches(writer.head['state']['policy'], manifest)


def status(state, descriptor, manifest):
    from tools.serve_recovery import lifecycle
    if manifest.get('recovery_profiles') is None:
        return dict(enabled=False, model_actions=False)
    value = lifecycle.read_family(Path(state), descriptor, profile_store.Store)
    matches(value, manifest)
    return dict(profile_policy.describe(value), enabled=True,
                configuration=value['configuration'])

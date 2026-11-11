"""Explicit STOPPED profile bootstrap under existing deployment/core locks."""
from pathlib import Path

from tools.serve_recovery import access_store, ledger, profile_contract
from tools.serve_recovery import profile_policy, profile_store


def matches(state, manifest):
    registry = manifest.get('recovery_profiles')
    ledger.require(registry is not None, 'package has no qualified profiles')
    profile_policy.validate(state)
    ledger.require(state['registry_sha256'] == profile_contract.digest(registry) and
        state['profiles'] == {n: ledger.digest(v) for n, v in registry['profiles'].items()} and
        state['configuration'] == manifest['recovery_profile_policy'],
        'profile journal differs from sealed qualification/policy')
    return state


def compatible(old, new):
    if old.get('recovery_profiles') is not None:
        ledger.require(old['recovery_profiles'] == new.get('recovery_profiles') and
            old['recovery_profile_policy'] == new.get('recovery_profile_policy'),
            'deployment would discard qualified profile state; qualification migration required')


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

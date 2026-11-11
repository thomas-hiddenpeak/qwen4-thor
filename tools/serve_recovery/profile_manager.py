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


def migration_store(grants):
    """Only exact durable version/deployment receipts may cross registries."""
    class Contract:
        MAX_BYTES = profile_policy.MAX_BYTES
        validate = staticmethod(profile_policy.validate)
        digest = staticmethod(profile_policy.digest)
        revision = staticmethod(profile_policy.revision)

        @staticmethod
        def authorized(previous, following, permits):
            for permit in permits:
                try:
                    clock = following['clock']
                    expected = profile_migration.candidate(previous, permit,
                        ledger.Stamp(clock['boot_id'], clock['monotonic_ns']))
                    if expected == following:return following
                except (ValueError, KeyError, TypeError):
                    pass
            raise ValueError('migration changed protected profile history')

        @classmethod
        def transition(cls, previous, following):
            try:return profile_policy.transition(previous, following)
            except (ValueError, KeyError, TypeError):
                return cls.authorized(previous, following,
                    [permit for permits in grants.values() for permit in permits])

    class AuthorizedStore(profile_store.Store):
        contract = Contract

        def reconcile(self):
            if self.exists('policy.pending') and self.exists('policy.json'):
                previous = self.envelope('policy.json')['state']['policy']
                pending = self.envelope('policy.pending')['state']
                following = pending['policy']
                if any(previous[k] != following[k]
                       for k in ('registry_sha256', 'configuration', 'profiles')):
                    request = pending['receipts'][-1]['id']
                    ledger.require(request in grants,
                        'pending profile migration belongs to another deployment or version switch')
                    Contract.authorized(previous, following, grants[request])
            super().reconcile()
    return AuthorizedStore


def current_source(current, sources):
    for package, manifest in reversed(sources):
        try:matches(current, manifest)
        except ValueError:continue
        return package, manifest
    raise ValueError('profile journal differs from registered repair predecessors')


def preflight(binding, old, new, *, root, core):
    """Before publishing an intent, verify the target can read current history."""
    if old.get('recovery_profiles') is None:
        return
    from tools.serve_recovery import version_runtime
    sources, grants = version_runtime.repair_sources(binding, root, core, old)
    with access_store.open_bound(binding, migration_store(grants)) as writer:
        current = writer.head['state']['policy']
        _, old = current_source(current, sources)
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
    if old.get('recovery_profiles') is None:
        return initialize(binding, new, core, previously_enabled=False)
    from tools.serve_recovery import version_runtime
    sources, grants = version_runtime.repair_sources(binding, intent['current'], core, old)
    request = str(uuid.uuid5(uuid.UUID(intent['request']), 'profile-migration'))
    permits = [compatible(manifest, new, package, intent['target']['package'])
               for package, manifest in sources]
    grants[request] = [permit for permit in permits if permit is not None]
    with access_store.open_bound(binding, migration_store(grants)) as writer:
        state = writer.head['state']
        current = state['policy']
        receipts = state['receipts']
        if any(r['id'] == request for r in receipts):
            ledger.require(receipts[-1]['id'] == request,
                           'profile state advanced during stopped deployment')
            matches(current, new)
            result = writer.transact(request, current['revision']-1, current)
        else:
            package, actual = current_source(current, sources)
            authorization = compatible(actual, new, package, intent['target']['package'])
            if authorization is None:
                matches(current, new)
                return None
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
    from tools.serve_recovery import bundle, version_launch
    version = version_launch.observed(state,descriptor)
    digest = descriptor['manifest_sha256']
    if version is not None:
        names = ('primary','fallback') if version['projection']['fallback_consumed'] else ('primary',)
        sources = []
        for name in names:
            item = version['registration']['payloads'][name]['descriptor']
            sources.append((item['package'],bundle.verify(item['package'],item['manifest_sha256'])))
        package, manifest = current_source(value,sources)
        digest = next(item['descriptor']['manifest_sha256'] for item in
            version['registration']['payloads'].values() if item['descriptor']['package'] == package)
    matches(value,manifest)
    return dict(profile_policy.describe(value),enabled=True,
        configuration=value['configuration'],profile_payload_manifest_sha256=digest,
        root_manifest_sha256=descriptor['manifest_sha256'])

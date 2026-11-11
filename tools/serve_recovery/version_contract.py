"""Verify sealed native payloads before an explicit recovery registration.

Paths locate retained packages, while the registry binds their complete file
identity and real primary/light business qualification. Registration grants no
launch permission and never changes live keys, quotas or application objects.
"""
import copy
from pathlib import Path

from tools.serve_recovery import bundle, ledger, profile_contract, profile_manager
from tools.serve_recovery import profile_compat, profile_migration, store, version_policy
from tools.serve_service import package as engine

FORMAT = 'q4t-version-registration-v1'


def descriptor(value):
    ledger.keys(value, ('package', 'manifest_sha256'))
    ledger.require(type(value['package']) is str and
        Path(value['package']).is_absolute() and len(value['package']) <= 4096,
        'invalid retained version package')
    version_policy.hash_value(value['manifest_sha256'])
    return value


def payload(value):
    descriptor(value)
    package = Path(value['package'])
    ledger.require(str(package.resolve(strict=True)) == value['package'],
                   'version package path must be canonical')
    manifest = bundle.verify(package, value['manifest_sha256'])
    ledger.require(manifest.get('ingress_mode') == 'native' and
        manifest.get('recovery_profiles') is not None and manifest['version'] >= 12,
        'recovery payload needs qualified native application service')
    base = engine.verify(package/'engine', manifest['engine_manifest_sha256'])
    settings = base['settings']
    qualified = manifest['recovery_profiles']
    bound = dict(binary_sha256=base['files']['q4t'], protocol_sha256=base['protocol_sha256'],
        port=settings['port'], operations_port=settings['operations_port'],
        profile_capacities={n:ledger.digest(v) for n,v in qualified['profiles'].items()},
        profile_registry_sha256=profile_contract.digest(qualified),
        controller_sha256=manifest['files']['source/tools/serve_recovery/controller.py'],
        ingress_sha256=manifest['files']['bin/q4t-ingress'])
    return dict(descriptor=copy.deepcopy(value), manifest=manifest, engine=base,
        binding=bound, evidence=profile_manager.evidence(package, manifest))


def compatible(first, second):
    old, new = first['manifest'], second['manifest']
    ledger.require(all(old[k] == new[k] for k in
        ('repository', 'lock_path', 'python', 'resource_mode', 'quota_enforced',
         'tls', 'tls_configuration')), 'recovery would change native access authority')
    a, b = first['engine'], second['engine']
    ledger.require(a['model_dir'] == b['model_dir'] and a['settings'] == b['settings'],
                   'automatic recovery would change application settings or capacity')
    # Current dynamic credential/quota journals remain authoritative. The
    # original immutable client/transport schema must still be identical.
    for name in ('gateway-policy.json', 'engine/client-policy.json'):
        left = engine.read(Path(first['descriptor']['package'])/name)
        right = engine.read(Path(second['descriptor']['package'])/name)
        ledger.require(left == right, 'recovery would change configured client/transport contract')
    return profile_migration.permit(old['recovery_profiles'], new['recovery_profiles'],
        first['evidence'], second['evidence'], old['recovery_profile_policy'],
        new['recovery_profile_policy'], automatic=True)


def accepted_fallback(receipt, selected):
    """Bind an actual accepted service origin, including a narrow state shim."""
    ledger.keys(receipt, ('release', 'decision'))
    values = {}
    for name, item in receipt.items():
        ledger.keys(item, ('path', 'sha256'))
        version_policy.hash_value(item['sha256'])
        path = Path(item['path'])
        ledger.require(path.is_absolute() and str(path.resolve(strict=True)) == item['path'] and
            engine.sha(path) == item['sha256'], 'retained service acceptance identity changed')
        values[name] = engine.read(path)
    metadata, decision = values['release'], values['decision']
    engine.accepted_release(metadata, decision)
    ledger.require(metadata.get('recovery_accepted') is True and
        metadata.get('acceptance_sha256') == receipt['decision']['sha256'] and
        decision.get('acceptance') == metadata['acceptance'] and
        decision.get('manifest_sha256') == metadata.get('manifest_sha256'),
        'fallback service was not accepted with its exact package')
    original = descriptor(dict(package=metadata['package'],
                               manifest_sha256=metadata['manifest_sha256']))
    origin = payload(original)
    ledger.require(metadata.get('engine_binary_sha256') == origin['binding']['binary_sha256'] and
        decision.get('engine_binary_sha256') == origin['binding']['binary_sha256'],
        'accepted fallback engine identity differs')
    business = decision.get('actual_business')
    ledger.require(type(business) is dict and type(business.get('status')) is str and
        business['status'].startswith(('ACCEPT_', 'PASS_')) and
        business.get('env_abort') is False and type(business.get('postflight')) is dict and
        all(business['postflight'].get(k) is True for k in
            ('sources_unchanged', 'owned_processes_gone', 'ports_free', 'lock_released')),
        'fallback needs retained real service business and cleanup evidence')
    if original != selected['descriptor']:
        try:
            profile_compat.verify(original['package'], original['manifest_sha256'],
                selected['descriptor']['package'], selected['descriptor']['manifest_sha256'])
        except ValueError:
            from tools.serve_recovery import cold_compat
            cold_compat.verify(original['package'], original['manifest_sha256'],
                selected['descriptor']['package'], selected['descriptor']['manifest_sha256'])
    return copy.deepcopy(receipt)


def prepare(root, primary, fallback, cycle_id, *, fallback_acceptance,
            bootstrap_boot_id=None):
    """Pure registration representation after full package verification.

    Persist only under STOPPED deployment/Guardian/core locks. A new cycle
    must be explicitly installed; runtime cannot reset a consumed fallback.
    The fixed root has its own descriptor, separate from the running payload.
    """
    descriptor(root)
    bootstrap_boot_id = store.stamp().boot_id if bootstrap_boot_id is None else bootstrap_boot_id
    ledger.require(ledger.identifier(bootstrap_boot_id), 'invalid frozen registration boot')
    root_package = Path(root['package'])
    ledger.require(str(root_package.resolve(strict=True)) == root['package'],
                   'version control root must be canonical')
    manifest = bundle.verify(root_package, root['manifest_sha256'])
    ledger.require(manifest.get('ingress_mode') == 'native', 'native version control root required')
    ledger.require(all('source/tools/serve_recovery/'+name+'.py' in manifest['files']
        for name in ('version_journal', 'version_store', 'version_policy')),
        'version control root lacks the native isolated selection contract')
    first, second = payload(primary), payload(fallback)
    if manifest['version'] >= 17 and 'bin/q4t-device-control' in manifest['files']:
        from tools.serve_recovery import cold_compat
        ledger.require(all(cold_compat.aware(item['descriptor']['package']) for item in (first, second)),
                       'device-domain recovery requires a cold-aware retained controller')
    ledger.require(all(manifest[k] == first['manifest'][k] for k in
        ('repository', 'lock_path', 'python')), 'version recovery root authority differs')
    permit = compatible(first, second)
    acceptance = accepted_fallback(fallback_acceptance, second)
    policy = version_policy.registry(dict(format=version_policy.REGISTRY_FORMAT,
        cycle_id=cycle_id, root_manifest_sha256=root['manifest_sha256'],
        payloads={name:dict(manifest_sha256=item['descriptor']['manifest_sha256'],
                           binding_sha256=ledger.digest(item['binding']))
                  for name,item in [('primary',first),('fallback',second)]},
        compatibility_sha256=ledger.digest(permit),
        qualification_sha256=ledger.digest(dict(service_acceptance=acceptance,
            profiles={name:{kind:record['sha256']
            for kind,record in item['evidence'].items()}
            for name,item in [('primary',first),('fallback',second)]}))))
    value = dict(format=FORMAT, root=copy.deepcopy(root), registry=policy,
        bootstrap_boot_id=bootstrap_boot_id,
        fallback_acceptance=acceptance,
        payloads={name:dict(descriptor=item['descriptor'], binding=item['binding'])
                  for name,item in [('primary',first),('fallback',second)]})
    ledger.require(len(ledger.canonical(value)) <= 16*1024, 'version registration byte bound')
    return value


def verify(value):
    ledger.keys(value, ('format', 'root', 'registry', 'payloads', 'fallback_acceptance',
                        'bootstrap_boot_id'))
    ledger.require(value['format'] == FORMAT, 'invalid version registration')
    version_policy.registry(value['registry'])
    ledger.keys(value['payloads'], version_policy.PAYLOADS)
    for item in value['payloads'].values():
        ledger.keys(item, ('descriptor', 'binding'))
    expected = prepare(value['root'], value['payloads']['primary']['descriptor'],
        value['payloads']['fallback']['descriptor'], value['registry']['cycle_id'],
        fallback_acceptance=value['fallback_acceptance'],
        bootstrap_boot_id=value['bootstrap_boot_id'])
    ledger.require(value == expected, 'version registration differs from retained actual packages')
    return value

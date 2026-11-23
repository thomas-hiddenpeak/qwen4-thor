"""Seal actual qualified observations with a profile-capable service package."""
from pathlib import Path
import shutil

from tools.serve_recovery import ledger, profile_contract, profile_evidence, profile_policy
from tools.serve_recovery import profile_record
from tools.serve_service import package as engine


def compatible(base, assets, registry):
    profile_contract.validate(registry)
    expected = profile_contract.binding(base['files']['q4t'], base['protocol_sha256'],
        base['model_files'], assets, base['settings'].get('native_embedding'),
        base['settings'].get('native_asr'), base['settings'].get('native_tts'),
        base['settings'].get('native_tts_base'))
    ledger.require(registry['identity'] == expected and
        registry['profiles']['primary'] == base['settings']['profile'],
        'qualified profiles do not belong to this engine/assets/capacity')


def verify_record(path, registry, name, base, *, ingress_sha256, require_live=True):
    value = profile_record.read(path)
    digest = engine.sha(path)
    profile_evidence.validate(value, registry, name, digest)
    live = value['business'].get('live')
    if live is not None or (require_live and base.get('live_workers') is not None):
        ledger.require(live is not None and base.get('live_workers') == live['workers'],
                       'installed Live workers differ from the qualified profile')
        from tools.serve_recovery import profile_live_reuse
        profile_live_reuse.verify_ingress(live, ingress_sha256)
    return digest


def prepare(target, base, assets, path, configuration):
    if path is None:
        ledger.require(configuration is None, 'profile policy needs qualified profiles')
        return None, None, []
    path = Path(path)
    registry = profile_contract.validate(engine.read(path))
    compatible(base, assets, registry)
    configuration = profile_policy.configuration(
        profile_policy.DEFAULTS if configuration is None else configuration)
    names = []
    ingress_sha256 = engine.sha(Path(target)/'bin/q4t-ingress') if base.get('live_workers') else None
    for name in profile_contract.NAMES:
        source = path.parent/(name+'.json')
        digest = verify_record(source, registry, name, base, ingress_sha256=ingress_sha256)
        filename = 'profile-'+name+'.json'
        destination = Path(target)/filename
        shutil.copyfile(source, destination)
        destination.chmod(0o400)
        ledger.require(engine.sha(destination) == digest, 'profile evidence changed during preparation')
        names.append(filename)
    return registry, configuration, names


def verify(package, manifest, base):
    registry = manifest.get('recovery_profiles')
    configuration = manifest.get('recovery_profile_policy')
    if registry is None:
        ledger.require(configuration is None, 'profile policy lacks qualification')
        return
    ledger.require(manifest['version'] >= 12 and manifest.get('ingress_mode') == 'native',
                   'qualified recovery requires managed native ingress')
    compatible(base, manifest['model_assets'], registry)
    profile_policy.configuration(configuration)
    for name in profile_contract.NAMES:
        path = Path(package)/('profile-'+name+'.json')
        verify_record(path, registry, name, base, ingress_sha256=manifest['files'].get('bin/q4t-ingress'),
                      require_live=manifest['version'] >= 28)

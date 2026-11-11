"""Seal actual qualified observations with a profile-capable service package."""
from pathlib import Path
import shutil

from tools.serve_recovery import ledger, profile_contract, profile_evidence, profile_policy
from tools.serve_service import package as engine


def compatible(base, assets, registry):
    profile_contract.validate(registry)
    expected = profile_contract.binding(base['files']['q4t'], base['protocol_sha256'],
        base['model_files'], assets, base['settings'].get('native_embedding'))
    ledger.require(registry['identity'] == expected and
        registry['profiles']['primary'] == base['settings']['profile'],
        'qualified profiles do not belong to this engine/assets/capacity')


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
    for name in profile_contract.NAMES:
        source = path.parent/(name+'.json')
        digest = engine.sha(source)
        profile_evidence.validate(engine.read(source), registry, name, digest)
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
        profile_evidence.validate(engine.read(path), registry, name, engine.sha(path))

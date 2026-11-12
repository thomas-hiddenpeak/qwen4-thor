"""Qualified capacity pairs; no model action, probing or live state I/O.

Only MTP changes in the first light profile. A qualification binds observations
and retained evidence; enabling recovery still requires the durable runtime
state machine and Guardian authority, never this declaration alone.
"""
import copy

from tools.serve_recovery import ledger
from tools.serve_service import capacity, package, supervisor

FORMAT = 'q4t-recovery-profiles-v1'
NAMES = ('primary', 'light')
MIB = 1024**2


def profiles(primary):
    primary = capacity.validate(primary)
    ledger.require(primary['mtp_enabled'], 'primary profile must enable MTP')
    return dict(primary=primary, light=dict(primary, mtp_enabled=False))


def profile_pair(value):
    ledger.keys(value, NAMES)
    expected = profiles(value['primary'])
    capacity.validate(value['light'])
    ledger.require(package.same_json(value, expected),
                   'light profile may only disable MTP')
    return copy.deepcopy(value)


def capabilities(value, enabled):
    """Keep every feature field except the qualified internal MTP switch."""
    ledger.require(type(enabled) is bool and type(value) is dict and
        type(value.get('runtime')) is dict and
        type(value['runtime'].get('mtp_enabled')) is bool and
        value['runtime']['mtp_enabled'] == enabled,
        'actual MTP capability observation differs')
    result = copy.deepcopy(value)
    del result['runtime']['mtp_enabled']
    return result


def capability_pair(primary, light):
    first, second = capabilities(primary, True), capabilities(light, False)
    ledger.require(package.same_json(first, second),
                   'light profile changes declared application capabilities')
    return ledger.digest(first)


def binding(binary, protocol, model_files, model_assets, native_embedding=None):
    """Asset paths can relocate; actual fixed model identities cannot change."""
    for value in (binary, protocol):
        ledger.require(type(value) is str and ledger.HASH.fullmatch(value),
                       'invalid qualified engine identity')
    ledger.require(type(model_files) is dict and 1 <= len(model_files) <= 256,
                   'invalid qualified model metadata inventory')
    for name, digest in model_files.items():
        ledger.require(type(name) is str and name.endswith('.json') and
            '/' not in name and '\\' not in name and len(name) <= 256 and
            type(digest) is str and ledger.HASH.fullmatch(digest),
            'invalid qualified model metadata identity')
    ledger.require({'config.json', 'tokenizer.json', 'tokenizer_config.json'} <=
                   model_files.keys(), 'incomplete qualified model identity')
    ledger.require(type(model_assets) is dict and 1 <= len(model_assets) <= 256,
                   'invalid qualified weight inventory')
    for name, entry in model_assets.items():
        ledger.require(type(name) is str and 1 <= len(name) <= 4096 and
            not any(p in ('', '.', '..') for p in name.split('/')) and
            '\\' not in name and '\0' not in name,
            'invalid qualified weight path')
        ledger.keys(entry, ('dev', 'ino', 'size', 'mtime_ns', 'ctime_ns'))
        ledger.require(all(ledger.integer(value) for value in entry.values()) and
                       entry['ino'] > 0 and entry['size'] > 0,
                       'invalid qualified weight identity')
    ledger.require(any(n.endswith('.safetensors') for n in model_assets) and
                   any(n.startswith('ple/') for n in model_assets),
                   'incomplete qualified weight inventory')
    native = None
    if native_embedding is not None:
        package.embedding_settings(native_embedding)
        native = {key:native_embedding[key] for key in package.NATIVE_EMBEDDING}
    return dict(binary_sha256=binary, protocol_sha256=protocol,
                model_files=copy.deepcopy(model_files),
                model_assets=copy.deepcopy(model_assets), native_embedding=native)


def configuration(profile, observed):
    """Use the real independent classifier's effective-capacity contract.

    The observation is supervisor.probe().configuration, not requested CLI
    fields. Only explicit capacities count; clipping cannot qualify a profile.
    """
    capacity.validate(profile)
    ledger.require(type(observed) is dict, 'missing effective configuration')
    expected = supervisor.expected_configuration(profile)
    ledger.require(all(key in observed and package.same_json(observed[key], value)
                       for key,value in expected.items()),
                   'qualified effective capacity differs')
    capacity.observation(profile, observed)
    return copy.deepcopy(expected)


def startup_memory(value):
    """Actual admitted startup estimate including the configured host margin."""
    ledger.keys(value, ('state', 'estimate_bytes', 'headroom_bytes',
        'required_bytes', 'observed_total_bytes', 'observed_available_bytes'))
    ledger.require(value['state'] == 'admitted' and all(
        ledger.integer(value[key]) for key in value if key != 'state') and
        value['estimate_bytes'] > 0 and
        value['required_bytes'] == value['estimate_bytes']+value['headroom_bytes'] and
        value['required_bytes'] <= value['observed_available_bytes'] <=
        value['observed_total_bytes'], 'invalid qualified memory admission')
    return value['required_bytes']


def identity(value):
    ledger.keys(value, ('binary_sha256', 'protocol_sha256', 'model_files',
                       'model_assets', 'native_embedding'))
    native = value['native_embedding']
    # The qualification omits deployment paths; reuse fixed-asset validation
    # with a synthetic absolute path without reading any file.
    if native is not None:
        ledger.keys(native, package.NATIVE_EMBEDDING)
        native = dict(native, directory='/qualified/assets')
    return binding(value['binary_sha256'], value['protocol_sha256'],
                   value['model_files'], value['model_assets'], native)


def qualification(name, selected, fixed, capability, observed, memory, evidence):
    """Construct a receipt from exact observed values and retained evidence.

    The caller must already have accepted actual business and retained its
    original evidence. This pure helper does not execute or certify that work.
    Package preparation must verify the evidence digest before enabling it.
    """
    ledger.require(name in NAMES, 'unknown qualified profile')
    fixed = identity(fixed)
    configuration(selected, observed)
    normalized = capabilities(capability, selected['mtp_enabled'])
    ledger.require(capability.get('protocol_source_sha256') ==
                   fixed['protocol_sha256'], 'qualified protocol observation differs')
    ledger.require(type(evidence) is str and ledger.HASH.fullmatch(evidence),
                   'invalid retained qualification evidence digest')
    return dict(profile=name, identity_sha256=ledger.digest(fixed),
        capacity_sha256=ledger.digest(selected),
        capabilities_sha256=ledger.digest(normalized),
        startup_required_bytes=startup_memory(memory), evidence_sha256=evidence)


def validate(value):
    ledger.keys(value, ('format', 'identity', 'profiles', 'qualifications'))
    ledger.require(value['format'] == FORMAT, 'unknown recovery profiles format')
    fixed = identity(value['identity'])
    selected = profile_pair(value['profiles'])
    receipts = value['qualifications']
    ledger.keys(receipts, NAMES)
    for name in NAMES:
        row = receipts[name]
        ledger.keys(row, ('profile', 'identity_sha256', 'capacity_sha256',
            'capabilities_sha256', 'startup_required_bytes', 'evidence_sha256'))
        ledger.require(row['profile'] == name and
            row['identity_sha256'] == ledger.digest(fixed) and
            row['capacity_sha256'] == ledger.digest(selected[name]) and
            ledger.integer(row['startup_required_bytes'], minimum=1),
            'profile qualification identity differs')
        for key in ('capabilities_sha256', 'evidence_sha256'):
            ledger.require(type(row[key]) is str and ledger.HASH.fullmatch(row[key]),
                           'invalid profile qualification digest')
    ledger.require(receipts['primary']['capabilities_sha256'] ==
                   receipts['light']['capabilities_sha256'],
                   'qualified profiles have different application capabilities')
    ledger.require(receipts['primary']['evidence_sha256'] !=
                   receipts['light']['evidence_sha256'],
                   'one qualification cannot stand in for both actual profiles')
    ledger.require(receipts['light']['startup_required_bytes'] <
                   receipts['primary']['startup_required_bytes'],
                   'light profile has no observed startup memory reduction')
    ledger.require(len(ledger.canonical(value)) <= 64*1024,
                   'qualified profile declaration byte bound')
    return value


def digest(value):
    return ledger.digest(validate(value))

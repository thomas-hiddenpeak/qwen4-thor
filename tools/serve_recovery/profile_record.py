"""Bounded qualification I/O; ordinary service metadata keeps its 2 MiB limit."""
import json
import os
from pathlib import Path

from tools.serve_recovery import ledger
from tools.serve_service import package

MAX_BYTES = 8 << 20
LEGACY_BYTES = 2 << 20


def shape(value, size):
    ledger.require(size <= MAX_BYTES, 'profile evidence size limit')
    if size > LEGACY_BYTES:
        ledger.require(type(value) is dict and type(value.get('reused_business')) is dict and
            ((value.get('format') == 'q4t-profile-business-v18' and
            value['reused_business'].get('format') in (
                'q4t-prompt-profile-source-reuse-v1',
                'q4t-prompt-refusal-source-reuse-v1',
                'q4t-prompt-refusal-source-assembly-v1')) or
             (value.get('format') == 'q4t-profile-business-v19' and
              value['reused_business'].get('format') in (
                  'q4t-speech-source-reuse-v1',
                  'q4t-speech-cancellation-source-reuse-v1')) or
             (value.get('format') == 'q4t-profile-business-v20' and
              value['reused_business'].get('format') in (
                  'q4t-duplex-source-reuse-v1',
                  'q4t-duplex-composed-source-reuse-v1')) or
             (value.get('format') == 'q4t-profile-business-v21' and
              value['reused_business'].get('format') == 'q4t-mcp-profile-source-reuse-v1') or
             (value.get('format') == 'q4t-profile-business-v22' and
              value['reused_business'].get('format') == 'q4t-tracing-profile-source-reuse-v1') or
             (value.get('format') == 'q4t-profile-business-v23' and
              value['reused_business'].get('format') == 'q4t-live-profile-source-reuse-v1')),
            'only explicit reviewed source records have the larger evidence bound')


def read(path):
    package.regular(path)
    with Path(path).open('rb') as stream:
        raw = stream.read(MAX_BYTES + 1)
    ledger.require(len(raw) <= MAX_BYTES, 'profile evidence size limit')

    def unique(pairs):
        result = {}
        for key, item in pairs:
            ledger.require(key not in result, 'duplicate profile evidence key')
            result[key] = item
        return result

    value = json.loads(raw, object_pairs_hook=unique,
        parse_constant=lambda _: ledger.require(False, 'non-finite profile evidence'))
    shape(value, len(raw))
    return value


def save(path, value):
    path = Path(path)
    raw = ledger.canonical(value) + b'\n'
    shape(value, len(raw))
    temporary = path.with_name(path.name + '.new')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    ledger.require(read(path) == value, 'profile evidence did not round trip')


def accepted_source(plan_path, manifest, primary, payload):
    """Bind managed actors to verified original or reviewed profile records."""
    from tools.serve_recovery import profile_evidence
    plan_path = Path(plan_path)
    registry_path = plan_path.parent / 'qualified/registry.json'
    registry = package.read(registry_path)
    accepted = package.read(plan_path.parent / 'acceptance.json')
    formats = {
        'ACCEPT_PRIMARY_LIGHT_QUALIFICATION': None,
        'ACCEPT_TARGETED_REFUSAL_PRIMARY_LIGHT': 'q4t-prompt-refusal-source-reuse-v1',
        'ACCEPT_REVIEWED_REFUSAL_PRIMARY_LIGHT': 'q4t-prompt-refusal-source-assembly-v1',
        'ACCEPT_SPEECH_PRIMARY_LIGHT': 'q4t-speech-source-reuse-v1',
        'ACCEPT_SPEECH_CANCELLATION_PRIMARY_LIGHT':
            'q4t-speech-cancellation-source-reuse-v1',
        'ACCEPT_DUPLEX_PRIMARY_LIGHT': 'q4t-duplex-source-reuse-v1',
        'ACCEPT_COMPOSED_DUPLEX_PRIMARY_LIGHT':
            'q4t-duplex-composed-source-reuse-v1',
        'ACCEPT_MCP_PRIMARY_LIGHT': 'q4t-mcp-profile-source-reuse-v1',
        'ACCEPT_LIVE_PRIMARY_LIGHT': 'q4t-live-profile-source-reuse-v1',
        'ACCEPT_TRACING_PRIMARY_LIGHT': 'q4t-tracing-profile-source-reuse-v1',
    }
    status = accepted.get('status')
    ledger.require(status in formats and
        accepted.get('registry_sha256') == package.sha(registry_path) and
        package.same_json(registry, manifest['recovery_profiles']),
        'managed profile acceptance or registry identity differs')
    records = {}
    for name in ('primary', 'light'):
        path = plan_path.parent / 'qualified' / (name + '.json')
        value = read(path)
        profile_evidence.validate(value, registry, name, package.sha(path))
        expected = formats[status]
        ledger.require(expected is None or
            value.get('reused_business', {}).get('format') == expected,
            'managed acceptance does not describe the retained evidence format')
        records[name] = value
    ledger.require(package.same_json(records['primary'], primary) and
        package.same_json(records['light'], read(Path(payload) / 'profile-light.json')),
        'managed payload differs from accepted profile records')
    return package.read(plan_path)

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
              value['reused_business'].get('format') == 'q4t-live-profile-source-reuse-v1') or
             (value.get('format') == 'q4t-profile-business-v24' and
              value['reused_business'].get('format') == 'q4t-live-beta-profile-source-reuse-v1') or
             (value.get('format') == 'q4t-profile-business-v25' and
              value['reused_business'].get('format') in (
                  'q4t-agent-profile-source-reuse-v1', 'q4t-agent-profile-requalification-v1')) or
             (value.get('format') == 'q4t-profile-business-v26' and
              value['reused_business'].get('format') == 'q4t-agent-registration-profile-source-v1') or
             (value.get('format') == 'q4t-profile-business-v27' and
              value['reused_business'].get('format') == 'q4t-agent-session-profile-source-v1') or
             (value.get('format') == 'q4t-profile-business-v28' and
              value['reused_business'].get('format') == 'q4t-agent-vault-profile-source-v1') or
             (value.get('format') == 'q4t-profile-business-v47' and
              value['reused_business'].get('format') == 'q4t-project-safety-source-v1') or
             (value.get('format') == 'q4t-profile-business-v46' and
              value['reused_business'].get('format') == 'q4t-eval-cosine-source-v1') or
             (value.get('format') == 'q4t-profile-business-v45' and
              value['reused_business'].get('format') == 'q4t-eval-source-v1') or
             (value.get('format') == 'q4t-profile-business-v44' and
              value['reused_business'].get('format') == 'q4t-chatkit-source-v1') or
             (value.get('format') == 'q4t-profile-business-v43' and
              value['reused_business'].get('format') == 'q4t-hosted-preparation-source-v1') or
             (value.get('format') == 'q4t-profile-business-v42' and
              value['reused_business'].get('format') == 'q4t-hosted-environment-lifecycle-source-v1') or
             (value.get('format') == 'q4t-profile-business-v41' and
              value['reused_business'].get('format') == 'q4t-webhook-source-v1') or
             (value.get('format') == 'q4t-profile-business-v40' and
              value['reused_business'].get('format') == 'q4t-agent-automation-source-v1') or
             (value.get('format') == 'q4t-profile-business-v39' and
              value['reused_business'].get('format') == 'q4t-translation-transport-source-v1') or
             (value.get('format') == 'q4t-profile-business-v38' and
              value['reused_business'].get('format') == 'q4t-interpreter-workspace-source-v1') or
             (value.get('format') == 'q4t-profile-business-v37' and
              value['reused_business'].get('format') == 'q4t-skill-workspace-source-v1') or
             (value.get('format') == 'q4t-profile-business-v36' and
              value['reused_business'].get('format') == 'q4t-assistants-run-source-v1') or
             (value.get('format') == 'q4t-profile-business-v35' and
              value['reused_business'].get('format') == 'q4t-translation-realtime-source-v1') or
             (value.get('format') == 'q4t-profile-business-v34' and
              value['reused_business'].get('format') == 'q4t-managed-subagent-source-v1') or
             (value.get('format') == 'q4t-profile-business-v33' and
              value['reused_business'].get('format') == 'q4t-managed-session-tools-source-v1') or
             (value.get('format') == 'q4t-profile-business-v32' and
              value['reused_business'].get('format') == 'q4t-session-artifact-source-v1') or
             (value.get('format') == 'q4t-profile-business-v31' and
              value['reused_business'].get('format') == 'q4t-agent-workspace-source-v1') or
             (value.get('format') == 'q4t-profile-business-v30' and
              value['reused_business'].get('format') == 'q4t-agent-environment-template-source-v1') or
             (value.get('format') == 'q4t-profile-business-v29' and
              value['reused_business'].get('format') == 'q4t-vault-oauth-profile-source-v1')),
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
        'ACCEPT_RESPONSES_BETA_PRIMARY_LIGHT': 'q4t-live-beta-profile-source-reuse-v1',
        'ACCEPT_HOSTED_AGENTS_PRIMARY_LIGHT': 'q4t-agent-profile-source-reuse-v1',
        'ACCEPT_AGENT_RECOVERY_PRIMARY_LIGHT': 'q4t-agent-profile-requalification-v1',
        'ACCEPT_AGENT_REGISTRATION_PRIMARY_LIGHT': 'q4t-agent-registration-profile-source-v1',
        'ACCEPT_AGENT_SESSIONS_PRIMARY_LIGHT': 'q4t-agent-session-profile-source-v1',
        'ACCEPT_AUTOMATION_PRIMARY_LIGHT': 'q4t-agent-automation-source-v1',
        'ACCEPT_PROJECT_SAFETY_PRIMARY_LIGHT': 'q4t-project-safety-source-v1',
        'ACCEPT_EVAL_COSINE_PRIMARY_LIGHT': 'q4t-eval-cosine-source-v1',
        'ACCEPT_EVAL_PRIMARY_LIGHT': 'q4t-eval-source-v1',
        'ACCEPT_CHATKIT_PRIMARY_LIGHT': 'q4t-chatkit-source-v1',
        'ACCEPT_HOSTED_PREPARATION_PRIMARY_LIGHT': 'q4t-hosted-preparation-source-v1',
        'ACCEPT_HOSTED_ENVIRONMENT_LIFECYCLE_PRIMARY_LIGHT': 'q4t-hosted-environment-lifecycle-source-v1',
        'ACCEPT_WEBHOOK_PRIMARY_LIGHT': 'q4t-webhook-source-v1',
        'ACCEPT_TRANSLATION_TRANSPORTS_PRIMARY_LIGHT': 'q4t-translation-transport-source-v1',
        'ACCEPT_INTERPRETER_PRIMARY_LIGHT': 'q4t-interpreter-workspace-source-v1',
        'ACCEPT_SKILLS_PRIMARY_LIGHT': 'q4t-skill-workspace-source-v1',
        'ACCEPT_ASSISTANTS_PRIMARY_LIGHT': 'q4t-assistants-run-source-v1',
        'ACCEPT_TRANSLATION_REALTIME_PRIMARY_LIGHT': 'q4t-translation-realtime-source-v1',
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
        # The original v24 publisher used the v23 label. Only the exact two
        # registered records have this historical interpretation.
        if status == 'ACCEPT_LIVE_PRIMARY_LIGHT' and value.get('format') == 'q4t-profile-business-v24':
            from tools.serve_recovery import profile_live_reuse
            ledger.require(package.sha(path) == profile_live_reuse.BETA_REVIEWED.get(name),
                           'unrecognized historical Beta qualification')
            expected = profile_live_reuse.BETA_FORMAT
        ledger.require(expected is None or
            value.get('reused_business', {}).get('format') == expected,
            'managed acceptance does not describe the retained evidence format')
        records[name] = value
    ledger.require(package.same_json(records['primary'], primary) and
        package.same_json(records['light'], read(Path(payload) / 'profile-light.json')),
        'managed payload differs from accepted profile records')
    return package.read(plan_path)

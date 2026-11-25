"""Check that an Agent's release lookup, cache lifetime and stream survive a serving profile change."""
from pathlib import Path
import re
import shutil

from tools.serve_recovery import profile_live_reuse as reuse
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_service import package

PROOF = lifecycle.ROOT / 'docs/evidence/serve-agent-session-controls-20261126/business.json'
RECOVERY = lifecycle.ROOT / 'docs/evidence/serve-agent-turn-output-recovery-20261126/business.json'
RELEASE = lifecycle.ROOT / 'docs/evidence/serve-chatkit-widget-stream-20261126/delivery.json'
ACTOR = Path(__file__).with_name('agent_session_control_profile_sdk.mjs')
STRUCTURED = lifecycle.ROOT / 'docs/evidence/serve-agent-structured-format-20261126/business.json'
STRUCTURED_ACTOR = Path(__file__).with_name('managed_session_recovery_sdk.mjs')
KEY = 'agent_session_control_handoff'
RECEIPT = 'agent-session-control-handoff.json'


def prepare(out, build, deployment, *, failed_store=None):
    proof = package.read(PROOF)
    identity = dict(binary=package.sha(build/'q4t'), ingress=package.sha(build/'q4t-ingress'))
    package.require(proof['https']['status'] == 'PASS' and proof['stored_policies']['request_and_effective_ttl']
                    == [[{'ttl':'12h'}, '12h'], [{'ttl':'30m'}, '30m'], [None,'30m']],
                    'session profiles require recorded request and effective lifetimes')
    drivers = [PROOF, RELEASE, ACTOR, Path(__file__), Path(lifecycle.__file__)]
    structured = None
    if identity != proof['identity']:
        recovery = package.read(RECOVERY)
        accepted = recovery['accepted']
        package.require(recovery['previous_candidate'] == proof['identity']
                        and accepted['https']['status'] == 'PASS'
                        and accepted['storage']['failed_record_identical']
                        and accepted['sdk']['peer'] == 404 and accepted['sdk']['answer'] == '42',
                        'session profiles require a verified candidate and its failed-turn replay')
        drivers.append(RECOVERY)
        if identity != recovery['identity']:
            structured = package.read(STRUCTURED)
            package.require(structured['identity'] == identity and structured['previous_candidate'] == recovery['identity']
                            and structured['https']['status'] == 'PASS' and failed_store is not None,
                            'structured Agent qualification requires a verified build and retained failed turn')
            drivers.extend([STRUCTURED, STRUCTURED_ACTOR])
    lifecycle.prepare_candidate(out, build, deployment, RELEASE, reuse.CHATKIT_WIDGET_REVIEWED,
                                KEY, drivers)
    if structured is not None:
        target = out/'state/application-objects'
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        package.require(not any(target.iterdir()), 'failed-turn replay needs a fresh private store')
        for name, digest in structured['verified']['original_failed_turn_unchanged'].items():
            source = Path(failed_store)/name
            package.require(Path(name).name == name and package.sha(source) == digest, 'failed record changed')
            shutil.copy2(source, target/name)
            package.require(package.sha(target/name) == digest, 'incomplete failed-turn copy')
        for name in ('primary', 'light'):
            path = out/name/'plan.json'
            plan = package.read(path)
            plan[KEY]['failed'] = structured['public_sdk']['historical']
            package.save(path, plan)


def check(row, previous=None):
    revision = str(package.read(RELEASE)['publication']['publication_revision'])
    source = row['source']
    prefixes = dict(session='agent_session_', turn='turn_', item='msg_')
    package.require(source.keys() == prefixes.keys() and all(isinstance(source[k], str)
                    and re.fullmatch(p+'[0-9a-f]{32}', source[k]) for k,p in prefixes.items()),
                    'invalid saved Agent session, turn or item identity')
    package.require(row['sdk'] == '7.47.0' and row['chat'] == '42' and row['revision'] == revision
                    and row['restored_ttl'] == '12h' and row['peer'] == 404
                    and type(row['deltas']) is int and 0 < row['deltas'] <= 256,
                    'invalid scoped session history or cache lifetime')
    if previous is None:
        package.require(row['phase'] == 'primary' and row['padded'] is False and 'resumed' not in row,
                        'invalid unpadded session creation')
        return
    check(previous)
    resume = row['resumed']
    package.require(row['phase'] == 'light' and source == previous['source'] and row['padded'] is True
                    and resume['answer'] == '42' and resume['ttl'] == '30m' and resume['cleared'] is True
                    and all(isinstance(resume[k], str) and re.fullmatch(prefixes[k]+'[0-9a-f]{32}', resume[k])
                            and resume[k] != source[k] for k in ('turn', 'item')),
                    'session recovery lost history, stream padding or future turn policy')


def check_structured(row, previous=None):
    check(row, previous)
    proof = package.read(STRUCTURED)['public_sdk']
    package.require(row['format'] == proof['format'] and row['historical'] == proof['historical'],
                    'structured Agent schema or historical failed turn changed')

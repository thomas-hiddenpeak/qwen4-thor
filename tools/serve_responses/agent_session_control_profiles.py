"""Check that an Agent's release lookup, cache lifetime and stream survive a serving profile change."""
from pathlib import Path
import re

from tools.serve_recovery import profile_live_reuse as reuse
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_service import package

PROOF = lifecycle.ROOT / 'docs/evidence/serve-agent-session-controls-20261126/business.json'
RELEASE = lifecycle.ROOT / 'docs/evidence/serve-chatkit-widget-stream-20261126/delivery.json'
ACTOR = Path(__file__).with_name('agent_session_control_profile_sdk.mjs')
KEY = 'agent_session_control_handoff'
RECEIPT = 'agent-session-control-handoff.json'


def prepare(out, build, deployment):
    proof = package.read(PROOF)
    package.require(proof['identity'] == dict(binary=package.sha(build/'q4t'), ingress=package.sha(build/'q4t-ingress'))
                    and proof['https']['status'] == 'PASS' and proof['stored_policies']['request_and_effective_ttl']
                    == [[{'ttl':'12h'}, '12h'], [{'ttl':'30m'}, '30m'], [None,'30m']],
                    'session profiles require the engine that recorded the three turn lifetimes')
    lifecycle.prepare_candidate(out, build, deployment, RELEASE, reuse.CHATKIT_WIDGET_REVIEWED,
                                KEY, [PROOF, RELEASE, ACTOR, Path(__file__), Path(lifecycle.__file__)])


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

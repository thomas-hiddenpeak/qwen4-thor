"""Validate environment-template and session-trace continuity on both capacities."""
import re
from pathlib import Path

from tools.serve_responses.beta_profile_acceptance import protocol
from tools.serve_service import package as pkg

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT/'docs/evidence/serve-agent-environments-20261124/template-network.json'
PUBLICATION = ROOT/'docs/evidence/serve-agent-vaults-20261123/oauth-delivery.json'
HELPER = 'tools/serve_responses/environment_profile_acceptance.py'
SDK = 'tools/serve_responses/environment_template_sdk.mjs'
SOURCES = (HELPER, SDK, str(EVIDENCE.relative_to(ROOT)), str(PUBLICATION.relative_to(ROOT)))
TEMPLATES = dict(selector='agents=v1', operations=['create','retrieve','update','list','delete'],
                 confidential_definition='sealed', default_network='disabled')
TRACES = dict(selector='agents=v1', endpoint='/v1/agents/sessions/{session_id}/traces',
              format='otlp_json', anchor='root_turn_id', published_only=True)


def candidate(build, current):
    proof = pkg.read(EVIDENCE)
    current_protocol = protocol(current)
    pkg.require(proof['business']['status'] == 'PASS' and proof['sdk']['foreign_file'] == 404 and
                proof['sdk']['rejected_network_policies'] == 3 and proof['sdk']['exact_blocked_domain'] and
                proof['driver'] == pkg.sha(ROOT/SDK) and proof['engine'] == pkg.sha(build/'q4t') and
                proof['protocol'] == current_protocol and current_protocol.encode() in (build/'q4t').read_bytes(),
                'managed templates require the tested network-policy engine and SDK')
    return dict(receipt_sha256=pkg.sha(EVIDENCE), binary=proof['engine'], ingress=proof['ingress'],
                protocol=current_protocol, renewal=True, environment_templates=True)


def verify(phase, plan, capabilities, row):
    handoff = row['template_trace']
    pkg.require(capabilities.get('agent_environment_templates') == TEMPLATES and
                capabilities.get('agent_session_traces') == TRACES and
                all(plan['sources'].get(key) == pkg.sha(ROOT/key) for key in SOURCES) and
                handoff['template_id'].startswith('environment_template_') and
                all(re.fullmatch('[0-9a-f]{64}', handoff[key]) for key in ('template_hash','waiting_step')) and
                re.fullmatch('[0-9a-f]{32}', handoff['trace_id']),
                'missing template or durable step evidence')
    if plan['name'] == 'light':
        prior = pkg.read(phase.parent/'primary/session-handoff.json')['template_trace']
        pkg.require(handoff == dict(prior, closed_spans=3, foreign=404, template_deleted=True),
                    'light did not recover the same waiting span and confidential template')

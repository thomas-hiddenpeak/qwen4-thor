"""Validate environment-template and session-trace continuity on both capacities."""
import re
from pathlib import Path

from tools.serve_responses.beta_profile_acceptance import protocol
from tools.serve_responses import subagent_profile_acceptance as subagents
from tools.serve_service import package as pkg

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT/'docs/evidence/serve-agent-environments-20261124/template-network.json'
PUBLICATION = ROOT/'docs/evidence/serve-agent-vaults-20261123/oauth-delivery.json'
HELPER = 'tools/serve_responses/environment_profile_acceptance.py'
SDK = 'tools/serve_responses/environment_template_sdk.mjs'
SOURCES = (HELPER, SDK, str(EVIDENCE.relative_to(ROOT)), str(PUBLICATION.relative_to(ROOT)))
WORKSPACE_PROOF = ROOT/'docs/evidence/serve-agent-workspaces-20261124/workspace.json'
WORKSPACE_PAYLOAD_PROOF = WORKSPACE_PROOF.with_name('payload-retention.json')
SOURCE_FILES_PROOF = WORKSPACE_PROOF.with_name('source-files.json')
SESSION_WEB_PROOF = WORKSPACE_PROOF.with_name('session-web.json')
SESSION_WEB_SDK = 'tools/serve_responses/agent_session_web_sdk.mjs'
EMPTY_CATALOG_PROOF = WORKSPACE_PROOF.with_name('empty-catalog.json')
DISCOVERY_SDK = 'tools/serve_responses/agent_session_tool_search_sdk.mjs'
WORKSPACE_PROOFS = (WORKSPACE_PROOF, WORKSPACE_PROOF.with_name('retention.json'),
                    WORKSPACE_PAYLOAD_PROOF, SOURCE_FILES_PROOF, SESSION_WEB_PROOF, EMPTY_CATALOG_PROOF, subagents.PROOF)
SOURCE_FILES_SDK = 'tools/serve_responses/environment_source_files_sdk.mjs'
RETENTION_SDK = 'tools/serve_responses/environment_retention_sdk.mjs'
WORKSPACE_RELEASE = EVIDENCE.with_name('delivery.json')
WORKSPACE_SDK = 'tools/serve_responses/environment_workspace_sdk.mjs'
WORKSPACE_CAPABILITY = dict(selector='agents=v1', prewarm_seconds=300, idempotency_seconds=86400,
                            workspace_files=True, network=['disabled'], between_turn_writes=True)
TEMPLATES = dict(selector='agents=v1', operations=['create','retrieve','update','list','delete'],
                 confidential_definition='sealed', default_network='disabled')
TRACES = dict(selector='agents=v1', endpoint='/v1/agents/sessions/{session_id}/traces',
              format='otlp_json', anchor='root_turn_id', published_only=True)


def workspace_proof(binary):
    matches = [path for path in WORKSPACE_PROOFS if pkg.read(path)['engine'] == binary]
    pkg.require(len(matches) == 1, 'workspace binary requires one matching reviewed SDK witness')
    return matches[0]


def workspace_sources(proof):
    if proof == subagents.PROOF:
        return (subagents.SDK, subagents.STANDALONE_SDK, subagents.HELPER, str(proof.relative_to(ROOT)),
                str(subagents.PUBLICATION.relative_to(ROOT)))
    if proof == EMPTY_CATALOG_PROOF:
        return (*workspace_sources(SESSION_WEB_PROOF), DISCOVERY_SDK, str(proof.relative_to(ROOT)))
    if proof == SESSION_WEB_PROOF:
        return (SESSION_WEB_SDK, str(proof.relative_to(ROOT)),
                str(workspace_release(proof).relative_to(ROOT)))
    if proof == SOURCE_FILES_PROOF:
        return (SOURCE_FILES_SDK, str(proof.relative_to(ROOT)),
                str(workspace_release(proof).relative_to(ROOT)))
    sources = (WORKSPACE_SDK, str(proof.relative_to(ROOT)), str(WORKSPACE_RELEASE.relative_to(ROOT)))
    return (*sources, RETENTION_SDK) if proof == WORKSPACE_PAYLOAD_PROOF else sources


def workspace_release(proof):
    if proof == subagents.PROOF:
        return subagents.PUBLICATION
    if proof in (SESSION_WEB_PROOF, EMPTY_CATALOG_PROOF):
        return WORKSPACE_PROOF.with_name('artifact-delivery.json')
    return WORKSPACE_PROOF.with_name('delivery.json') if proof == SOURCE_FILES_PROOF else WORKSPACE_RELEASE


def candidate(build, current, workspaces=False):
    if workspaces:
        source = workspace_proof(pkg.sha(build/'q4t'))
        if source == subagents.PROOF:
            return subagents.candidate(build, protocol(current))
        proof = pkg.read(source)
        if source in (SESSION_WEB_PROOF, EMPTY_CATALOG_PROOF):
            web = pkg.read(SESSION_WEB_PROOF)
            modes = web['sdk']['phases']
            pkg.require(web['business']['status'] == 'PASS' and web['lease'] == 'COMPLETED' and
                        web['driver'] == pkg.sha(ROOT/SESSION_WEB_SDK) and
                        web['source']['source_receipt_sha256'] == pkg.sha(workspace_release(source)) and
                        web['source']['connections'] == 1 and
                        all(modes[k]['foreign'] == 404 for k in ('live','cached','disabled')) and
                        modes['live']['actions'] and modes['cached']['actions'] and
                        modes['disabled']['actions'] == [] and modes['disabled']['answer'] == '42',
                        'managed session web witness differs')
            pkg.require(proof['engine'] == pkg.sha(build/'q4t') and proof['protocol'] == protocol(current)
                        and proof['ingress'] == web['ingress'], 'managed session binary drift')
            if source == EMPTY_CATALOG_PROOF:
                name = 'src/server/agent_session_turn.cpp'
                prior = dict(current, **{name:proof['previous_source_sha256']})
                pkg.require(proof['business']['status'] == 'PASS' and proof['lease'] == 'COMPLETED' and
                            proof['driver'] == pkg.sha(ROOT/DISCOVERY_SDK) and
                            proof['web_witness'] == pkg.sha(SESSION_WEB_PROOF) and
                            current[name] != prior[name] and protocol(prior) == web['protocol'] and
                            proof['discovery']['same_function'] and proof['discovery']['search_after_revocation'] == 0
                            and proof['sdk']['results'] == 1 and proof['sdk']['foreign'] == 404,
                            'revoked catalog witness or earlier web protocol differs')
            return dict(receipt_sha256=pkg.sha(source), binary=proof['engine'], ingress=proof['ingress'],
                        protocol=proof['protocol'], renewal=True, environment_templates=True,
                        hosted_workspaces=True, session_artifacts=True, session_tools=True)
        if source == SOURCE_FILES_PROOF:
            result = proof['sdk']
            pkg.require(proof['business']['status'] == 'PASS' and proof['lease'] == 'COMPLETED' and
                        proof['engine'] == pkg.sha(build/'q4t') and proof['protocol'] == protocol(current) and
                        proof['driver'] == pkg.sha(ROOT/SOURCE_FILES_SDK) and
                        result['file_bytes'] == result['download_bytes'] == 50 << 20 and
                        result['deleted_source'] == result['foreign_source'] == result['deleted_session'] == 404 and
                        result['connected_rejected'] == result['prewarm_rejected'] == 400 and
                        result['answer'] == '42' and re.fullmatch('[0-9a-f]{64}',result['download_sha256']),
                        'large Workspace source and immutable artifact witness differs')
            return dict(receipt_sha256=pkg.sha(source), binary=proof['engine'], ingress=proof['ingress'],
                        protocol=proof['protocol'], renewal=True, environment_templates=True,
                        hosted_workspaces=True, session_artifacts=True)
        pkg.require(proof['business']['status'] == 'PASS' and proof['lease'] == 'COMPLETED' and
                    proof['sdk']['states'] == ['ready', 'connected', 'disconnected'] and
                    proof['sdk']['tenant'] == 404 and proof['sdk']['turn_answer'] == '42' and
                    proof['engine'] == pkg.sha(build/'q4t') and proof['protocol'] == protocol(current) and
                    proof['driver'] == pkg.sha(ROOT/WORKSPACE_SDK), 'workspace SDK witness drift')
        if source == WORKSPACE_PAYLOAD_PROOF:
            gc = proof['online_collection']
            pkg.require(gc['sdk'] == pkg.sha(ROOT/RETENTION_SDK) and gc['source_bytes'] == 5 << 20 and
                        gc['eligible'] == gc['deleted'] == 1 and gc['template_reopened'] and
                        gc['workspace_reopened'] and gc['disconnected'] and gc['replaced_bytes'] == 3,
                        'encrypted workspace did not survive the online sweep')
        return dict(receipt_sha256=pkg.sha(source), binary=proof['engine'],
                    ingress=proof['ingress'], protocol=proof['protocol'], renewal=True,
                    environment_templates=True, hosted_workspaces=True)
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
    if plan['agent_vaults'].get('session_tools'):
        primary = (row if plan['name'] == 'primary' else
                   pkg.read(phase.parent/'primary/session-handoff.json'))
        settings = primary['session_tools']
        prefix = settings['prefix']
        pkg.require(isinstance(prefix, list) and prefix and all(isinstance(i, str) and i for i in prefix)
                    and len(set(prefix)) == len(prefix)
                    and settings == dict(deferred=True, hidden=True, revoked=True, prefix=prefix),
                    'missing managed tool handoff prefix')
        if plan['name'] == 'light':
            settings = row['session_tools']
            indices = settings['indices']
            pkg.require(isinstance(indices, list) and indices and all(type(i) is int for i in indices)
                        and indices == list(range(len(prefix), len(prefix) + len(indices)))
                        and settings == dict(resumed=True, indices=indices, clock_checked=True),
                        'resumed stream indices did not follow earlier public outputs')
    if plan['agent_vaults'].get('hosted_workspaces'):
        space = row['workspace']
        sources = workspace_sources(workspace_proof(plan['binary_sha256']))
        pkg.require(capabilities.get('agent_environments') == WORKSPACE_CAPABILITY and
                    all(plan['sources'].get(k) == pkg.sha(ROOT/k) for k in sources) and
                    space['id'].startswith('environment_') and space['session'] == row['id'] and
                    re.fullmatch('[0-9a-f]{64}', space['files_digest']) and space['active_write'] == 409,
                    'incomplete connected workspace evidence')
        if plan['name'] == 'light':
            previous = pkg.read(phase.parent/'primary/session-handoff.json')['workspace']
            pkg.require(space == dict(previous, byte_length=0, disconnected=True),
                        'light did not recover the original workspace')
        if plan['agent_vaults'].get('session_artifacts'):
            from tools.serve_recovery.profile_live_reuse import ARTIFACT_CAPABILITY
            artifacts = row['artifacts']
            pkg.require(capabilities.get('agent_session_artifacts') == ARTIFACT_CAPABILITY and
                        artifacts['source_bytes'] == 384 << 10 and artifacts['deleted_source'] == 404 and
                        artifacts['waiting_count'] == 0 and re.fullmatch('[0-9a-f]{64}', artifacts['sha256']),
                        'missing encrypted workspace or artifact observation')
            if plan['name'] == 'light':
                before = pkg.read(phase.parent/'primary/session-handoff.json')['artifacts']
                pkg.require(artifacts == dict(before, published=2, distinct=True, matched=2, foreign=404,
                                              deleted=404), 'immutable artifact handoff differs')
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

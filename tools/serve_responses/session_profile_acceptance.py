"""Bind both managed capacities to an SDK handoff of one pending client function."""
import json
import os
from pathlib import Path
import re
import subprocess

from tools.serve_responses.beta_profile_acceptance import protocol
from tools.serve_service import package as pkg

ROOT = Path(__file__).resolve().parents[2]
RECEIPT = ROOT/'docs/evidence/serve-agent-sessions-20261123/admission.json'
PUBLICATION = ROOT/'docs/evidence/serve-agent-registration-20261123/delivery.json'
SDK = 'tools/serve_responses/agent_session_handoff_sdk.mjs'
HELPER = 'tools/serve_responses/session_profile_acceptance.py'
CAPABILITY = dict(selector='agents=v1', endpoint='/v1/agents/sessions',
    environments=['none'], tool_types=['function'], stream=True,
    idempotent_input_events=True, cancel_turn=True, storage='authenticated_client')


def candidate(build, witness, current):
    admission = pkg.read(RECEIPT)
    tested = pkg.read(witness/'compiled.json')
    first, final = protocol(tested), protocol(current)
    pkg.require(admission['lease'] == 'COMPLETED' and admission['outcome']['status'] == 'PASS'
                and admission['sdk']['saturated_submit'] == 429 and
                admission['sdk']['retry_accepted'] == 202,
                'session admission witness')
    pkg.require(pkg.sha(witness/'q4t') == admission['plan']['binary'] and
                tested.keys() == current.keys() and
                {p for p in current if tested[p] != current[p]} == {'src/server/chat_capabilities.cpp',
                    'src/server/agent_session_turn.cpp'} and
                first.encode() in (witness/'q4t').read_bytes() and
                final.encode() in (build/'q4t').read_bytes(),
                'expected only session declaration and turn error projection to change')
    return dict(witness=str(witness), receipt_sha256=pkg.sha(RECEIPT),
                tested_protocol=first, protocol=final, binary=pkg.sha(build/'q4t'),
                ingress=admission['plan']['ingress'])


def verify_identity(plan, capabilities):
    if 'agent_vaults' in plan:
        from tools.serve_responses import vault_profile_acceptance
        vault_profile_acceptance.verify_identity(plan, capabilities)
        pkg.require(plan['agent_sessions']['binary'] == plan['agent_vaults']['binary'] and
                    plan['sources'][SDK] == pkg.sha(ROOT/SDK) and
                    plan['sources'][str(PUBLICATION.relative_to(ROOT))] == pkg.sha(PUBLICATION),
                    'original session and publication witness drift')
        return
    selection = plan['agent_sessions']
    witness = Path(selection['witness'])
    binary = Path(plan['binary'])
    receipt = pkg.read(RECEIPT)
    pkg.require(selection['receipt_sha256'] == pkg.sha(RECEIPT) and
                pkg.sha(witness/'q4t') == receipt['plan']['binary'] and
                protocol(pkg.read(witness/'compiled.json')) == selection['tested_protocol'] and
                plan['binary_sha256'] == selection['binary'] == pkg.sha(binary) and
                plan['ingress_sha256'] == selection['ingress'] == receipt['plan']['ingress'] and
                capabilities.get('protocol_source_sha256') == selection['protocol'] and
                capabilities.get('agent_sessions') == CAPABILITY and
                plan['sources'][SDK] == pkg.sha(ROOT/SDK) and
                plan['sources'][str(PUBLICATION.relative_to(ROOT))] == pkg.sha(PUBLICATION),
                'session qualification does not bind this engine, ingress, SDK and release source')


def verify(phase, plan, capabilities):
    verify_identity(plan, capabilities)
    path = phase/'session-handoff.json'
    row = pkg.read(path)
    action = row['action']
    pkg.require(row['sdk'] == '7.47.0' and row['foreign'] == 404 and
                row['id'].startswith('agent_session_') and bool(action['turn_id']) and
                bool(action['call_id']) and action['name'] == 'read_release_record' and
                action['arguments'] == {} and action['type'] == 'function_call',
                'SDK did not observe the owned pending lookup')
    if plan['name'] == 'primary':
        pkg.require(row['status'] == 'requires_action', 'primary lost pending tool')
    else:
        previous = pkg.read(phase.parent/'primary/session-handoff.json')
        revision = pkg.read(PUBLICATION)['publication']['publication_revision']
        number = r'\b' + str(revision) + r'\b'
        pkg.require(row['id'] == previous['id'] and action == previous['action'] and
                    len({row['rejected_turn'], row['next_turn'], action['turn_id']}) == 3 and
                    row['quota_error'] == 'rate_limit_exceeded' and row['revision'] == revision and
                    row['completed'] == row['idle'] == row['result_count'] == 1 and row['replay'] == 202 and
                    re.search(number, row['answer']) and re.search(number, row['next_answer']),
                    'light failed to finish the original lookup and separate follow-up')
    return {path.name: pkg.sha(path)}


def business(stage, capabilities):
    verify_identity(stage.plan, capabilities)
    name = stage.plan['name']
    params = dict(base='https://127.0.0.1:'+str(stage.plan['public_port'])+'/v1',
                  key=stage.keys['alpha'], other=stage.keys['gamma'],
                  sdk=stage.plan['sdk_directory'], profile=name)
    if name == 'light':
        params['prior'] = pkg.read(stage.out.parent/'primary/session-handoff.json')
    result = subprocess.run(['node', ROOT/SDK], input=json.dumps(params), text=True,
        capture_output=True, env=dict(os.environ, NODE_EXTRA_CA_CERTS=stage.plan['tls']['certificate']),
        timeout=150)
    if result.returncode:
        (stage.out/'session-handoff.stderr').write_text(result.stderr)
        raise RuntimeError('session handoff SDK failed in '+name+'; see private session-handoff.stderr')
    pkg.save(stage.out/'session-handoff.json', json.loads(result.stdout))
    verify(stage.out, stage.plan, capabilities)

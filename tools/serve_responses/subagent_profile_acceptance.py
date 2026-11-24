"""Read the closed child on a different capacity before issuing any new generation."""
import json
import os
import re
import subprocess
from pathlib import Path

from tools.serve_service import package as pkg
from tools.serve_recovery.profile_live_reuse import SUBAGENT_CAPABILITY

ROOT = Path(__file__).resolve().parents[2]
SDK = 'tools/serve_responses/agent_subagent_profile_sdk.mjs'
STANDALONE_SDK = 'tools/serve_responses/agent_session_subagent_sdk.mjs'
HELPER = 'tools/serve_responses/subagent_profile_acceptance.py'
PROOF = ROOT/'docs/evidence/serve-agent-workspaces-20261124/subagent-lifecycle.json'
PUBLICATION = PROOF.with_name('session-tools-delivery.json')
QUOTA_PROOF = PROOF.with_name('subagent-quota.json')
QUOTA_SDK = 'tools/serve_responses/agent_quota_recovery_sdk.mjs'


def candidate(build, protocol, source=PROOF):
    proof = pkg.read(source)
    sdk = proof['sdk']
    pkg.require(proof['business']['status'] == 'PASS' and proof['lease'] == 'COMPLETED'
                and proof['previous_release_sha256'] == pkg.sha(PUBLICATION)
                and proof['engine'] == pkg.sha(build/'q4t')
                and proof['ingress'] == pkg.sha(build/'q4t-ingress')
                and proof['protocol'] == protocol and sdk['foreign'] == 404,
                'standalone subagent identity or ownership differs')
    if source == QUOTA_PROOF:
        pkg.require(sdk['original_error']['code'] == 'invalid_request' and sdk['limit'] == 4096
                    and 0 < sdk['actual_input'] <= sdk['limit']
                    and sdk['parent_answer'] == sdk['child_answer'] == '457'
                    and sdk['child_turns'] == 2 and sdk['new_tool_calls'] == 0 and sdk['idle'] == 1
                    and sdk['original_data_untouched'] and sdk['independent_chat'] == '42'
                    and sdk['driver_sha256'] == pkg.sha(ROOT/QUOTA_SDK),
                    'actual subagent quota continuation differs')
    else:
        closed, active = sdk['transitions']
        pkg.require(source == PROOF and proof['driver'] == pkg.sha(ROOT/STANDALONE_SDK)
                    and closed['state'] == 'closed' and active['state'] == 'active'
                    and closed['closed_at'] >= closed['opened_at'] == active['opened_at']
                    and active['closed_at'] is None and sdk['secondAnswer'] == str(sdk['revision'] + 1)
                    and proof['independent_chat']['answer'] == '42', 'standalone subagent lifecycle differs')
    return dict(receipt_sha256=pkg.sha(source), binary=proof['engine'], ingress=proof['ingress'], protocol=protocol,
                renewal=True, environment_templates=True, hosted_workspaces=True, session_artifacts=True,
                session_tools=True, subagents=True)


def verify(phase, plan, capabilities):
    row = pkg.read(phase/'subagent-handoff.json')
    closed = row['closed']
    turns = row['turns']
    revision = pkg.read(PUBLICATION)['publication']['publication_revision']
    primary = plan['name'] == 'primary'
    pkg.require(pkg.same_json(capabilities.get('agent_session_subagents'), SUBAGENT_CAPABILITY)
                and row['sdk'] == '7.47.0' and row['foreign'] == 404 and row['revision'] == revision
                and row['session'].startswith('agent_session_') and row['child'].startswith('subagent_')
                and closed['id'] == row['child'] and closed['status'] == 'closed' and closed['name'] == 'reader'
                and closed['closed_at'] >= closed['opened_at'] and len(turns) == (1 if primary else 2)
                and len({t['id'] for t in turns}) == len(turns)
                and all(t['id'].startswith('turn_') and t['status'] == 'completed'
                        and re.fullmatch('[0-9a-f]{64}',t['items_sha256'])
                        and str(revision + i) in re.findall(r'\d+', t['answer']) for i,t in enumerate(turns)),
                'subagent ownership, original release, or turn memory differs')
    if primary:
        pkg.require('active' not in row, 'primary must leave the child closed')
    else:
        previous = pkg.read(phase.parent/'primary/subagent-handoff.json')
        pkg.require(all(row[k] == previous[k] for k in ('session','child','closed','revision'))
                    and turns[:1] == previous['turns']
                    and row['active'] == dict(closed, status='active', closed_at=None),
                    'light did not recover the original closed child and history')
    return {'subagent-handoff.json':pkg.sha(phase/'subagent-handoff.json')}


def business(stage):
    name = stage.plan['name']
    params = dict(base=f"https://127.0.0.1:{stage.plan['public_port']}/v1", key=stage.keys['gamma'],
                  other=stage.keys['beta'], sdk=stage.plan['sdk_directory'], profile=name,
                  revision=pkg.read(PUBLICATION)['publication']['publication_revision'])
    if name == 'light':
        params['prior'] = pkg.read(stage.out.parent/'primary/subagent-handoff.json')
    result = subprocess.run(['node',ROOT/SDK], input=json.dumps(params), text=True, capture_output=True,
        env=dict(os.environ, NODE_EXTRA_CA_CERTS=stage.plan['tls']['certificate']), timeout=125)
    if result.returncode:
        (stage.out/'subagent-handoff.stderr').write_text(result.stderr)
        raise RuntimeError('subagent handoff failed on '+name+'; see private subagent-handoff.stderr')
    pkg.save(stage.out/'subagent-handoff.json', json.loads(result.stdout))

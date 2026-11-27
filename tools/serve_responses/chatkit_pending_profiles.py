"""Verify a pending browser tool call across a primary to light handoff."""
import json
import os
from pathlib import Path
import re
import subprocess

from tools.serve_recovery import profile_live_reuse as reuse, profile_record
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_service import package

ROOT = lifecycle.ROOT
ACTOR = Path(__file__).with_name('chatkit_pending_message_sdk.mjs')
PROOF = ROOT/'docs/evidence/serve-chatkit-pending-user-message-20261127/combined.json'
KEY = 'chatkit_pending_browser_handoff'
RECEIPT = 'chatkit-pending-handoff.json'
IDENTITY = {'workflow': 'agent_', 'thread': 'cthr_', 'call': 'call_',
            'original_user': 'item_', 'original_item': 'fc_'}


def prepare(out, build, deployment):
    proof = package.read(PROOF)
    package.require(proof['engine'] == package.sha(build/'q4t')
                    and proof['compiled_index_sha256'] == package.sha(
                        build/'generated/q4t/server/chat_protocol_sources.json'),
                    'combined engine differs from its reviewed sources')
    lifecycle.prepare_from_manifest(out, build, deployment, proof['predecessor_manifest'],
                                    reuse.ASSET_RETRIEVAL_REVIEWED, KEY,
                                    [PROOF, Path(__file__), ACTOR])


def check(row, name, previous=None):
    package.require(name in ('primary', 'light') and row['phase'] == name
                    and row['sdk'] == '7.47.0'
                    and all(isinstance(row.get(k), str) and row[k].startswith(prefix)
                            for k, prefix in IDENTITY.items())
                    and re.fullmatch('[a-f0-9]{64}', row['original_sha256']),
                    'invalid ChatKit pending call identity')
    if name == 'primary':
        package.require(previous is None and row['requests'] == 4 and 'answer' not in row
                        and 'history' not in row, 'primary must leave the call pending')
        return
    package.require(previous is not None, 'missing primary browser observation')
    check(previous, 'primary')
    package.require(all(row[k] == previous[k] for k in (*IDENTITY, 'original_sha256')),
                    'new session did not reopen the original pending call')
    history = row['history']
    package.require(row['requests'] == 10 and row['answer'] == '42'
                    and len(history) == len(set(history)) == 4
                    and history[:2] == [row['original_user'], row['original_item']]
                    and history[2].startswith('item_') and history[3].startswith('msg_')
                    and row['unchanged_pending_call'] is True and row['late_output'] == 409
                    and row['foreign_session'] == row['foreign_project'] == 404,
                    'incomplete browser history, interruption or ownership check')


def business(stage):
    name = stage.plan['name']
    previous = package.read(stage.out.parent/'primary'/RECEIPT) if name == 'light' else None
    args = dict(base=f"https://127.0.0.1:{stage.plan['public_port']}/v1",
                key=stage.keys['alpha'], other=stage.keys['beta'], phase=name,
                sdk=stage.plan['sdk_directory'], previous=previous)
    with (stage.out/'pending-sdk.stderr').open('xb') as errors:
        result = subprocess.run(['node', ACTOR], input=json.dumps(args).encode(),
                                stdout=subprocess.PIPE, stderr=errors, timeout=125,
                                env=dict(os.environ,
                                         NODE_EXTRA_CA_CERTS=stage.plan['tls']['certificate']))
    package.require(result.returncode == 0, 'pending browser handoff failed; see pending-sdk.stderr')
    row = json.loads(result.stdout)
    check(row, name, previous)
    package.save(stage.out/RECEIPT, row)


def verify(phase, plan, capabilities, current):
    name = plan['name']
    previous_file = phase/'previous-profile.json'
    previous = profile_record.read(previous_file)
    proof = package.read(PROOF)
    change = plan[KEY]
    package.require(package.sha(previous_file) == change['previous'] == reuse.ASSET_RETRIEVAL_REVIEWED[name]
                    and proof['predecessor_manifest'] == change['release'] == plan['selected_sha256']
                    and proof['engine'] == plan['binary_sha256']
                    and plan['sources'][str(PROOF.relative_to(ROOT))] == package.sha(PROOF)
                    and previous['capacity'] == plan['capacity']
                    and package.same_json(capabilities, dict(previous['capabilities'],
                                                        protocol_source_sha256=change['protocol'])),
                    'the tested binary, released capacity or protocol changed')
    first = package.read(phase.parent/'primary'/RECEIPT) if name == 'light' else None
    check(package.read(phase/RECEIPT), name, first)
    inherited = previous['business']['live']['evidence_sha256']
    package.require(reuse.ASSET_RETRIEVAL_RECEIPT in inherited, 'reviewed retrieval evidence missing')
    return {**{k:v for k,v in inherited.items() if k not in current}, RECEIPT:package.sha(phase/RECEIPT)}

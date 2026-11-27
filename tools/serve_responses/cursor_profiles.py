"""Qualify durable Batch, Vector Store and voice consent list boundaries."""
import json
import os
from pathlib import Path
import re
import subprocess

from tools.serve_recovery import profile_live_reuse as reuse, profile_record
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_service import package

ROOT = lifecycle.ROOT
PROOF = ROOT/'docs/evidence/serve-voice-consent-pagination-20261127/device.json'
RELEASE = ROOT/'docs/evidence/serve-agent-turn-cache-20261127/delivery.json'
ACTOR = Path(__file__).with_name('cursor_handoff_sdk.mjs')
KEY = 'cursor_page_handoff'
RECEIPT = 'cursor-page-handoff.json'


def prepare(out, build, deployment):
    proof = package.read(PROOF)
    previous = package.read(RELEASE)['publication']
    package.require(proof['engine'] == package.sha(build/'q4t')
                    and proof['prior_revision'] == previous['publication_revision']
                    and proof['independent_chat'] == '42',
                    'candidate or deployment differs from the device qualification')
    lifecycle.prepare_candidate(out, build, deployment, RELEASE,
                                reuse.AGENT_TURN_CACHE_REVIEWED, KEY,
                                [PROOF, RELEASE, ACTOR, Path(__file__)])


def check(row, name, previous=None):
    package.require(name in ('primary', 'light') and (previous is None) == (name == 'primary')
                    and row['sdk'] == '7.47.0' and row['foreign_404'] == 2
                    and row['requests'] == (15 if name == 'primary' else 10)
                    and row['upload_probes'] == (1 if name == 'primary' else 0),
                    'missing cursor SDK or owner isolation observation')
    for key, prefix in [('store', 'vs'), ('file', 'file'), ('batch', 'vsfb')]:
        package.require(re.fullmatch(prefix + '_[a-f0-9]{32}', row[key]),
                        'invalid durable cursor object id')
    empty = ['', '']
    expected = dict(batches=empty, consents=[None, None], peer_stores=empty,
                    owned_stores=[row['store']]*2, files=[row['file']]*2,
                    members=[row['file']]*2, filtered_files=empty, filtered_members=empty)
    if previous is None:
        expected.update(initial_stores=empty, initial_files=empty)
    else:
        check(previous, 'primary')
        package.require(all(row[k] == previous[k] for k in ('store', 'file', 'batch')),
                        'light capacity did not reopen the original vector objects')
    package.require(row['pages'] == expected, 'cursor values differ across SDK pages')


def business(stage):
    plan = stage.plan
    name = plan['name']
    previous = package.read(stage.out.parent/'primary'/RECEIPT) if name == 'light' else None
    args = dict(base=f"https://127.0.0.1:{plan['public_port']}/v1", key=stage.keys['alpha'],
                other=stage.keys['beta'], sdk=plan['sdk_directory'], previous=previous)
    with (stage.out/'cursor-sdk.stderr').open('xb') as err:
        result = subprocess.run(['node', ACTOR], input=json.dumps(args).encode(),
                                stdout=subprocess.PIPE, stderr=err, timeout=100,
                                env=dict(os.environ, NODE_EXTRA_CA_CERTS=plan['tls']['certificate']))
    package.require(result.returncode == 0, 'cursor SDK failed; see cursor-sdk.stderr')
    row = json.loads(result.stdout)
    check(row, name, previous)
    package.save(stage.out/RECEIPT, row)


def verify(phase, plan, capabilities, current):
    name = plan['name']
    path = phase/'previous-profile.json'
    prior = profile_record.read(path)
    proof = package.read(PROOF)
    change = plan[KEY]
    package.require(package.sha(path) == change['previous'] == reuse.AGENT_TURN_CACHE_REVIEWED[name]
                    and change['release'] == plan['selected_sha256'] == package.read(RELEASE)['publication']['primary']
                    and plan['binary_sha256'] == proof['engine']
                    and plan['sources'][str(PROOF.relative_to(ROOT))] == package.sha(PROOF)
                    and prior['capacity'] == plan['capacity']
                    and package.same_json(capabilities, dict(prior['capabilities'],
                                                            protocol_source_sha256=change['protocol'])),
                    'candidate, capacities or reviewed predecessor differs')
    previous = package.read(phase.parent/'primary'/RECEIPT) if name == 'light' else None
    check(package.read(phase/RECEIPT), name, previous)
    inherited = prior['business']['live']['evidence_sha256']
    package.require(reuse.AGENT_TURN_CACHE_RECEIPT in inherited, 'previous Agent qualification missing')
    return {**{k:v for k,v in inherited.items() if k not in current}, RECEIPT:package.sha(phase/RECEIPT)}

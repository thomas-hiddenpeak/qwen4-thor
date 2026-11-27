"""Reopen delegated root and child turns in both accepted service capacities."""
import json
import os
import re
import subprocess
from pathlib import Path

from tools.serve_recovery import profile_live_reuse as reuse, profile_record
from tools.serve_responses import approval_identifiers_profiles, environment_lifecycle_profiles as lifecycle
from tools.serve_service import package

ROOT = lifecycle.ROOT
PROOF = ROOT/'docs/evidence/serve-agent-turn-cache-20261127/sdk-business.json'
STORE = PROOF.with_name('source-store.json')
RELEASE = ROOT/'docs/evidence/serve-request-defaults-20261127/delivery.json'
ACTOR = Path(__file__).with_name('agent_turn_cache_handoff_sdk.mjs')
KEY = 'agent_turn_cache_handoff'
RECEIPT = 'agent-turn-cache-handoff.json'


def prepare(out, build, deployment, source):
    proof = package.read(PROOF)
    witness = package.read(STORE)
    release = package.read(RELEASE)['publication']
    package.require(witness['foreground'] == package.sha(PROOF)
                    and proof['engine_sha256'] == package.sha(build/'q4t')
                    and proof['default_revision'] == release['publication_revision']
                    and proof['observations']['successful_writes_by_ttl'].keys() == {'43200', '1800'},
                    'cache handoff needs the original accepted device run')
    lifecycle.prepare_candidate(out, build, deployment, RELEASE, reuse.RECORDING_DOWNLOAD_REVIEWED,
                                KEY, [PROOF, STORE, RELEASE, ACTOR, Path(__file__)])
    store = out/'state/application-objects'
    approval_identifiers_profiles.seed(source, store, witness)
    for role in ('primary', 'light'):
        plan = package.read(out/role/'plan.json')
        args = plan['command']
        package.require(Path(args[args.index('--object-store-dir')+1]) == store,
                        'the two profiles must reopen the same restored object store')


def check(row, proof, name, previous=None):
    observed = proof['observations']
    revision = proof['default_revision']
    package.require(name in ('primary', 'light') and row['phase'] == name
                    and row['sdk'] == observed['sdk'] and row['revision'] == revision
                    and row['session'] == observed['session'] and row['child'] == observed['child']
                    and row['cross_client_404'] == 4 and len(row['rows']) == 2,
                    'cache handoff owner, SDK, isolation or turn count changed')
    for i, (item, turn) in enumerate(zip(row['rows'], observed['turns'], strict=True)):
        answer = [observed['first_answer'], observed['followup_answer']][i]
        package.require(all(item[k] == turn[k] for k in ('root', 'child', 'ttl'))
                        and item['answer'] == answer and str(revision+i) in re.findall(r'\d+', answer)
                        and all(re.fullmatch('[0-9a-f]{64}', item[k]) for k in
                                ('root_sha256','child_sha256','items_sha256')),
                        'historical turn selection, answer or resource digest differs')
    if name == 'primary':
        package.require(previous is None and row['next_ttl'] == '12h' and 'continuation' not in row,
                        'primary must persist the new session lifetime')
        return
    package.require(previous is not None, 'missing primary capacity observation')
    check(previous, proof, 'primary')
    later = row['continuation']
    historical = {item[k] for item in row['rows'] for k in ('root','child')}
    package.require(row['rows'] == previous['rows'] and later['ttl'] == previous['next_ttl']
                    and later['completed_sse'] == 2 and later['root'] != later['child']
                    and all(re.fullmatch('turn_[0-9a-f]{32}', later[k]) and later[k] not in historical
                            for k in ('root','child'))
                    and re.fullmatch('[0-9a-f]{64}', later['items_sha256'])
                    and str(revision+2) in re.findall(r'\d+', later['answer']),
                    'light profile lost original history or failed to continue the same child')


def business(stage):
    plan = stage.plan
    proof = package.read(PROOF)
    name = plan['name']
    previous = package.read(stage.out.parent/'primary'/RECEIPT) if name == 'light' else None
    args = dict(base=f"https://127.0.0.1:{plan['public_port']}/v1", key=stage.keys['alpha'],
                other=stage.keys['beta'], phase=name, sdk=plan['sdk_directory'], previous=previous,
                revision=proof['default_revision'], foreground=proof['observations'])
    with (stage.out/'agent-turn-cache-sdk.stderr').open('xb') as err:
        proc = subprocess.run(['node', ACTOR], input=json.dumps(args).encode(), stdout=subprocess.PIPE,
                              stderr=err, timeout=150,
                              env=dict(os.environ,NODE_EXTRA_CA_CERTS=plan['tls']['certificate']))
    package.require(proc.returncode == 0, 'delegated turn SDK failed; inspect phase stderr')
    row = json.loads(proc.stdout)
    check(row, proof, name, previous)
    package.save(stage.out/RECEIPT, row)


def verify(phase, plan, capabilities, current):
    name = plan['name']
    prior_file = phase/'previous-profile.json'
    prior = profile_record.read(prior_file)
    proof = package.read(PROOF)
    package.require(package.sha(prior_file) == reuse.RECORDING_DOWNLOAD_REVIEWED[name] == plan[KEY]['previous']
                    and plan['sources'][str(PROOF.relative_to(ROOT))] == package.sha(PROOF)
                    and plan['binary_sha256'] == proof['engine_sha256']
                    and plan[KEY]['release'] == plan['selected_sha256'] == package.read(RELEASE)['publication']['primary']
                    and prior['capacity'] == plan['capacity']
                    and package.same_json(capabilities, dict(prior['capabilities'],
                                                             protocol_source_sha256=plan[KEY]['protocol'])),
                    'unexpected delegated turn capacity, release or protocol identity')
    previous = package.read(phase.parent/'primary'/RECEIPT) if name == 'light' else None
    check(package.read(phase/RECEIPT), proof, name, previous)
    inherited = prior['business']['live']['evidence_sha256']
    package.require(reuse.RECORDING_DOWNLOAD_RECEIPT in inherited, 'original download evidence missing')
    return {**{k:v for k,v in inherited.items() if k not in current}, RECEIPT:package.sha(phase/RECEIPT)}

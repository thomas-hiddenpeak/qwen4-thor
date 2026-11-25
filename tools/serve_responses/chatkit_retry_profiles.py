"""Qualify retry of a primary-created ChatKit tool turn on the light profile."""
import json
import os
from pathlib import Path
import subprocess

from tools.serve_recovery import profile_live_reuse as reuse, profile_record
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_responses.assistant_profiles import COMMON
from tools.serve_service import package

ROOT = lifecycle.ROOT
RELEASE = ROOT / 'docs/evidence/serve-stored-message-projection-20261125/delivery.json'
PROOF = ROOT / 'docs/evidence/serve-chatkit-retry-20261125/business.json'
PAGES = ROOT / 'docs/evidence/serve-chatkit-page-defaults-20261125/business.json'
SCOPE = ROOT / 'docs/evidence/serve-scope-decision-20261120/decision.json'
ACTOR = Path(__file__).with_name('chatkit_retry_profile_sdk.mjs').resolve()
SOURCE_ACTOR = ACTOR.with_name('chatkit_handoff_sdk.mjs')
KEY = 'chatkit_retry_handoff'
RECEIPT = reuse.CHATKIT_RETRY_RECEIPT


def prepare(out, build, deployment):
    proof, pages = package.read(PROOF), package.read(PAGES)
    package.require(proof['result']['status'] == pages['run']['status'] == 'PASS' and
                    proof['result']['unchanged_default'] and pages['run']['unchanged_default'] and
                    proof['checksums']['handoff_driver'] == package.sha(SOURCE_ACTOR) and
                    proof['identity']['binary'] == package.sha(build / 'q4t') and
                    proof['stored_trace']['original_and_retry_snapshot_bindings_identical'],
                    'missing verified retry build, snapshot or pagination')
    lifecycle.prepare_candidate(out, build, deployment, RELEASE, reuse.STORED_REFUSAL_REVIEWED,
                                KEY, [PROOF, PAGES, SCOPE, RELEASE, ACTOR, SOURCE_ACTOR,
                                      Path(__file__), Path(lifecycle.__file__)])


def check_result(row, previous=None):
    source = row['source']
    expected = package.read(SCOPE)['counts']['required_inference_or_application_service']
    package.require(row['sdk'] == '7.47.0' and row['chat'] == '42' and
                    row['peer'] == row['deleted'] == source['source_deleted'] == 404 and
                    row['browser_zero'] == 2 and row['management_zero'] == 0 and
                    source['sha256'] == package.sha(SCOPE) and source['bytes'] == SCOPE.stat().st_size and
                    source['proposed_count'] == expected, 'source, scope or pagination changed')
    ids = row['original_ids']
    package.require(isinstance(ids, list) and len(set(ids)) == len(ids) == 2 and
                    ids[0].startswith('item_') and ids[1].startswith('fc_') and
                    all(source[k].startswith(prefix) for k, prefix in
                        [('thread','cthr_'), ('workflow','agent_'), ('call','call_'),
                         ('attachment','file_'), ('session','cksess_')]), 'missing pending turn identities')
    if previous is None:
        package.require(row['phase'] == 'primary' and 'recovered' not in row,
                        'primary must leave the call pending')
        return
    check_result(previous)
    result = row['recovered']
    final = result['ids']
    package.require(row['phase'] == 'light' and source == previous['source'] and
                    ids == previous['original_ids'] and result['foreign_browser'] == 404 and
                    result['answer'] == str(expected) and result['call'].startswith('call_') and
                    result['call'] != source['call'] and len(set(final)) == len(final) == 3 and
                    final[0] == ids[0] and final[1].startswith('fc_') and final[1] != ids[1] and
                    final[2].startswith('msg_'), 'retry did not replace the original pending call')


def verify(phase, plan, capabilities):
    prior_file = phase / 'previous-profile.json'
    prior = profile_record.read(prior_file)
    name, change = plan['name'], plan[KEY]
    expected = dict(prior['capabilities'], protocol_source_sha256=change['protocol'])
    package.require(package.sha(prior_file) == change['previous'] == reuse.STORED_REFUSAL_REVIEWED[name]
                    and plan['selected_sha256'] == change['release'] == package.read(RELEASE)['publication']['primary']
                    and prior['capacity'] == plan['capacity'] and package.same_json(expected, capabilities),
                    'unexpected change in release identity, capacity or runtime declarations')
    previous = package.read(phase.parent / 'primary' / RECEIPT) if name == 'light' else None
    check_result(package.read(phase / RECEIPT), previous)
    inherited = prior['business']['live']['evidence_sha256']
    package.require(COMMON <= inherited.keys() and reuse.STORED_REFUSAL_RECEIPT in inherited,
                    'missing reviewed predecessor evidence')
    return {**{k:v for k,v in inherited.items() if k not in COMMON}, RECEIPT: package.sha(phase / RECEIPT)}


def business(stage, capabilities):
    plan = stage.plan
    params = dict(base=f"https://127.0.0.1:{plan['public_port']}/v1",
                  key=stage.keys['alpha'], other=stage.keys['beta'], phase=plan['name'],
                  sdk=plan['sdk_directory'], model='qwen3.8-flash-next')
    if plan['name'] == 'light':
        params['previous'] = package.read(stage.out.parent / 'primary' / RECEIPT)
    with (stage.out / 'chatkit-retry-sdk.stderr').open('xb') as err:
        run = subprocess.run(['node', ACTOR], input=json.dumps(params), text=True,
                             stdout=subprocess.PIPE, stderr=err, timeout=150,
                             env=dict(os.environ, NODE_EXTRA_CA_CERTS=plan['tls']['certificate']))
    package.require(run.returncode == 0, 'ChatKit SDK failed; see chatkit-retry-sdk.stderr')
    package.save(stage.out / RECEIPT, json.loads(run.stdout))
    verify(stage.out, plan, capabilities)

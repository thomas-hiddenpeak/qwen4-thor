"""Qualify a widget and pending form across primary and light profiles."""
import json
import os
from pathlib import Path
import subprocess

from tools.serve_recovery import profile_live_reuse as reuse, profile_record
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_responses.assistant_profiles import COMMON
from tools.serve_service import package

ROOT = lifecycle.ROOT
RELEASE = ROOT / 'docs/evidence/serve-chatkit-retry-20261125/delivery.json'
PROOF = ROOT / 'docs/evidence/serve-chatkit-actions-20261125/business.json'
FORM = ROOT / 'docs/evidence/serve-chatkit-structured-20261125/business.json'
ACTOR = Path(__file__).with_name('chatkit_actions_profile_sdk.mjs').resolve()
KEY = 'chatkit_actions_handoff'
RECEIPT = reuse.CHATKIT_ACTIONS_RECEIPT


def prepare(out, build, deployment):
    proof = package.read(PROOF)
    combined = proof['combined']
    package.require(proof['run']['status'] == combined['run']['status'] == 'PASS' and
                    combined['run']['unchanged_default'] and proof['reference_operations'] == 16 and
                    len(set(proof['candidate_operations'])) == 16 and combined['actor'] == package.sha(ACTOR)
                    and combined['engine'] == package.sha(build/'q4t') and
                    package.read(FORM)['upstream_check']['persisted_rows_validated'] == 2,
                    'missing independently checked form, action, or combined engine')
    lifecycle.prepare_candidate(out, build, deployment, RELEASE, reuse.CHATKIT_RETRY_REVIEWED,
                                KEY, [PROOF, FORM, RELEASE, ACTOR, Path(__file__), Path(lifecycle.__file__)])


def check_result(row, previous=None):
    expected = str(package.read(RELEASE)['publication']['publication_revision'])
    package.require(row['sdk'] == '7.47.0' and row['chat'] == '42' and row['revision'] == expected,
                    'unexpected SDK, core chat or release')
    source = row['source']
    identifiers = dict(workflow='agent_', file='file_', thread='cthr_', widget='item_',
                       form='fc_', call='call_', user='item_')
    package.require(all(source[k].startswith(prefix) for k, prefix in identifiers.items())
                    and len({source[k] for k in identifiers}) == len(identifiers),
                    'missing original workflow, thread, widget or tool identity')
    if previous is None:
        package.require(row['phase'] == 'primary' and 'recovered' not in row,
                        'primary must leave the form pending')
        return
    check_result(previous)
    result = row['recovered']
    ids = result['ids']
    package.require(row['phase'] == 'light' and source == previous['source'] and
                    result['foreign'] == result['deleted'] == 404 and
                    result['new_turn'] == result['untyped'] == result['duplicate'] == 409 and
                    result['answered_form'] == source['form'] and result['call'] == source['call'] and
                    result['answer'] == expected and result['widget_label'] == 'verified ' + expected and
                    len(ids) == len(set(ids)) == 4 and ids[:3] == [source['user'],source['form'],source['widget']]
                    and ids[3].startswith('msg_'), 'persisted widget, form identity or answer changed')


def verify(phase, plan, capabilities):
    prior_file = phase / 'previous-profile.json'
    prior = profile_record.read(prior_file)
    name, change = plan['name'], plan[KEY]
    expected = dict(prior['capabilities'], protocol_source_sha256=change['protocol'])
    package.require(package.sha(prior_file) == change['previous'] == reuse.CHATKIT_RETRY_REVIEWED[name]
                    and plan['selected_sha256'] == change['release'] == package.read(RELEASE)['publication']['primary']
                    and prior['capacity'] == plan['capacity'] and package.same_json(expected, capabilities),
                    'unexpected change in release identity, capacity or runtime declarations')
    previous = package.read(phase.parent / 'primary' / RECEIPT) if name == 'light' else None
    check_result(package.read(phase / RECEIPT), previous)
    inherited = prior['business']['live']['evidence_sha256']
    package.require(COMMON <= inherited.keys() and reuse.CHATKIT_RETRY_RECEIPT in inherited,
                    'missing reviewed predecessor evidence')
    return {**{k:v for k,v in inherited.items() if k not in COMMON}, RECEIPT: package.sha(phase / RECEIPT)}


def business(stage, capabilities):
    plan = stage.plan
    params = dict(base=f"https://127.0.0.1:{plan['public_port']}/v1",
                  key=stage.keys['alpha'], other=stage.keys['beta'], phase=plan['name'],
                  sdk=plan['sdk_directory'], model='qwen3.8-flash-next')
    if plan['name'] == 'light':
        params['previous'] = package.read(stage.out.parent / 'primary' / RECEIPT)
    with (stage.out / 'chatkit-actions-sdk.stderr').open('xb') as err:
        run = subprocess.run(['node', ACTOR], input=json.dumps(params), text=True,
                             stdout=subprocess.PIPE, stderr=err, timeout=150,
                             env=dict(os.environ, NODE_EXTRA_CA_CERTS=plan['tls']['certificate']))
    package.require(run.returncode == 0, 'ChatKit SDK failed; see chatkit-actions-sdk.stderr')
    package.save(stage.out / RECEIPT, json.loads(run.stdout))
    verify(stage.out, plan, capabilities)

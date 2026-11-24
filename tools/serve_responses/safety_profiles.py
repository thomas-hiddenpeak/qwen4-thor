"""Recover reviewed project records and a file-backed Chat across capacities."""
import argparse
import json
import os
from pathlib import Path
import subprocess

from tools.serve_recovery import profile_live_reuse as reuse, profile_record
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_responses.assistant_profiles import COMMON
from tools.serve_service import package

ROOT = lifecycle.ROOT
PROOF = ROOT/'docs/evidence/serve-project-safety-20261125/records.json'
RELEASE = ROOT/'docs/evidence/serve-evals-20261125/cosine-delivery.json'
PDF_PROOF = RELEASE.with_name('stored-chat-files.json')
SCOPE = ROOT/'docs/evidence/serve-scope-decision-20261120/decision.json'
PDF = ROOT/'tests/fixtures/file_nested/page.pdf'
ACTORS = {'records': Path(__file__).with_name('safety_sdk.mjs').resolve(),
          'document': Path(__file__).with_name('eval_frozen_file_sdk.mjs').resolve()}
RECEIPT = 'project-safety-handoff.json'
KEY = 'project_safety_handoff'


def prepare(out, build, deployment):
    for proof in (PROOF, PDF_PROOF):
        package.require(package.read(proof)['device']['status'] == 'PASS',
                        'missing real business validation: '+proof.name)
    sources = (*ACTORS.values(), Path(__file__).resolve(), Path(lifecycle.__file__),
               PROOF, RELEASE, PDF_PROOF, SCOPE, PDF)
    lifecycle.prepare_candidate(out, build, deployment, RELEASE, reuse.COSINE_REVIEWED, KEY, sources)


def exercise(out, params, env, previous=None):
    row = {}
    for key, actor in ACTORS.items():
        options = dict(params, history='chat_pdf')
        if previous is not None:
            options['previous'] = previous[key]
        with (out/(key+'-sdk.stderr')).open('xb') as err:
            result = subprocess.run(['node', actor], input=json.dumps(options), text=True,
                                    stdout=subprocess.PIPE, stderr=err, timeout=125, env=env)
        package.require(result.returncode == 0, 'project handoff failed: '+key)
        observed = json.loads(result.stdout)
        if key == 'document':
            observed.pop('wire', None)
        row[key] = observed
    return row


def business(stage, capabilities):
    plan = stage.plan
    primary = plan['name'] == 'primary'
    previous = None if primary else package.read(stage.out.parent/'primary'/RECEIPT)
    command = plan['command']
    params = dict(base=f"https://127.0.0.1:{plan['public_port']}/v1", key=stage.keys['alpha'],
                  other=stage.keys['beta'], sdk=plan['sdk_directory'], phase=plan['name'],
                  model='qwen3.8-flash-next', engine=plan['binary'],
                  objects=command[command.index('--object-store-dir')+1])
    env = dict(os.environ, NODE_EXTRA_CA_CERTS=plan['tls']['certificate'])
    package.save(stage.out/RECEIPT, exercise(stage.out, params, env, previous))
    verify(stage.out, plan, capabilities)


def check_result(row, first, *, restored):
    records = row['records']
    doc = row['document']
    answer = str(package.read(SCOPE)['counts']['required_inference_or_application_service'])
    package.require(records == first['records'] and records['sdk'] == '7.47.0'
                    and records['independent_chat'] == '42' and records['answer'] == '72'
                    and records['foreign_status'] == 404 and records['excessive_id_status'] == 400,
                    'project record, associated model response or tenant scope differs')
    alert, case = records['wire']
    package.require((alert['path'], case['path']) == ('/safety/alerts/{id}', '/safety/cases/{id}')
                    and all(x['method'] == 'get' and x['status'] == 200 for x in (alert, case))
                    and alert['value']['request_id'] == records['approved_request']
                    and alert['value']['response_id'] == records['source_response']
                    and records['approved_alert'] == alert['value']['id']
                    and records['reviewed_case'] == case['value']['id'],
                    'published record identity differs')
    package.require(doc['sdk'] == '7.47.0' and doc['history_answer'] == answer
                    and doc['source_deleted_before_selection'] == 404
                    and all(doc[k] == first['document'][k] for k in
                            ('sha256', 'source_file', 'response', 'history_answer')),
                    'stored Chat or original file differs')
    if restored:
        package.require(doc['response_deleted'] == doc['foreign_project'] == 404
                        and doc['replay_answer'] == answer and doc['score'] == 1
                        and doc['independent_chat'] == '42' and doc['evaluation'] and doc['output'],
                        'source-deleted document replay did not complete under the second capacity')


def verify(phase, plan, capabilities):
    prior = profile_record.read(phase/'previous-profile.json')
    change = plan[KEY]
    expected = dict(prior['capabilities'], protocol_source_sha256=change['protocol'],
                    project_safety=reuse.SAFETY_CAPABILITY)
    name = plan['name']
    package.require(package.sha(phase/'previous-profile.json') == change['previous'] ==
                    reuse.COSINE_REVIEWED[name] and prior['capacity'] == plan['capacity'] and
                    package.same_json(expected, capabilities) and plan['selected_sha256'] ==
                    change['release'] == package.read(RELEASE)['publication']['primary'],
                    'project records require the issued capacity and exact capability extension')
    row = package.read(phase/RECEIPT)
    restored = name == 'light'
    first = package.read(phase.parent/'primary'/RECEIPT) if restored else row
    check_result(row, first, restored=restored)
    inherited = prior['business']['live']['evidence_sha256']
    package.require(COMMON <= inherited.keys() and 'eval-cosine-handoff.json' in inherited,
                    'missing issued Evals qualification')
    return {**{k:v for k,v in inherited.items() if k not in COMMON}, RECEIPT: package.sha(phase/RECEIPT)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('output', 'build', 'deployment'):
        parser.add_argument('--'+key, required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    package.require(output.is_relative_to(ROOT/'.q4t-work'), 'private evidence directory required')
    prepare(output, args.build.resolve(strict=True), args.deployment.resolve(strict=True))

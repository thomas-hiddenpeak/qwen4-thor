"""Verify the same Assistants pages and original Run across both service capacities."""

import argparse
import json
import os
from pathlib import Path
import subprocess

from tools.serve_audit import sdk_wire_receipt
from tools.serve_recovery import profile_live_reuse as reuse, profile_record
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_responses.assistant_profiles import COMMON
from tools.serve_service import package

ROOT = lifecycle.ROOT
PROOF = ROOT / 'docs/evidence/serve-assistant-pagination-20261125/http.json'
RELEASE = ROOT / 'docs/evidence/serve-chatkit-20261125/upload-delivery.json'
CONTRACT = ROOT / 'docs/serve-openapi-bundle-v1.json'
ACTOR = Path(__file__).with_name('assistant_pagination_sdk.mjs')
KEY = 'assistant_pagination_handoff'
RECEIPT = reuse.PAGINATION_RECEIPT
PAGES = {'/assistants', '/threads/{thread_id}/messages', '/threads/{thread_id}/runs',
         '/threads/{thread_id}/runs/{run_id}/steps'}


def prepare(out, build, deployment, official):
    proof = package.read(PROOF)
    package.require(proof['status'] == 'PASS' and proof['engine'] == package.sha(build/'q4t')
                    and proof['official_sha256'] == package.sha(official)
                    and proof['openapi_sha256'] == package.sha(CONTRACT),
                    'matching SDK page verification required')
    sources = (PROOF, RELEASE, CONTRACT, ACTOR, Path(__file__), Path(sdk_wire_receipt.__file__))
    lifecycle.prepare_candidate(out, build, deployment, RELEASE, reuse.UPLOAD_REVIEWED, KEY, sources)
    for name in ('primary', 'light'):
        path = out/name/'plan.json'
        plan = package.read(path)
        plan[KEY]['official'] = str(official)
        package.save(path, plan)


def check_result(row, previous=None):
    pages = row['pages']
    package.require(row['sdk'] == '7.47.0' and row['answer'] == row['independent_chat'] == '42'
                    and row['foreign_status'] == 404 and set(pages) == PAGES
                    and row['assistant'] in pages['/assistants']
                    and row['run'] in pages['/threads/{thread_id}/runs']
                    and all(isinstance(ids, list) and ids and len(set(ids)) == len(ids)
                            for ids in pages.values()),
                    'incorrect Run, page identities or client isolation')
    receipts = row['receipts']
    package.require(len(receipts) == (9 if previous else 12)
                    and {r['path'] for r in receipts} == PAGES | {'/chatkit/threads'}
                    and all(r['method'] == 'GET' and r['status'] == 200 for r in receipts),
                    'incomplete page responses')
    if previous:
        package.require(all(row[k] == previous[k] for k in ('assistant', 'thread', 'run', 'pages'))
                        and receipts[-8:] == previous['receipts'][-8:],
                        'restored owner, Run, messages, steps or terminal pages changed')


def exercise(out, params, env, official, previous=None):
    with (out/'pagination-sdk.stderr').open('xb') as err:
        run = subprocess.run(['node', ACTOR], input=json.dumps(dict(params, handoff=True,
                             previous=previous)), text=True, stdout=subprocess.PIPE, stderr=err,
                             timeout=150, env=env)
    package.require(run.returncode == 0, 'pagination SDK business failed')
    row = json.loads(run.stdout)
    wire = row.pop('wire')
    row['receipts'] = sdk_wire_receipt.validate(package.read_openapi(official), wire)
    local = sdk_wire_receipt.validate(package.read_openapi(CONTRACT),
        [dict(observed, path='/v1'+observed['path']) for observed in wire])
    package.require([r['json_sha256'] for r in local] == [r['json_sha256'] for r in row['receipts']],
                    'installed SDK results differ across the two contracts')
    check_result(row, previous)
    return row


def business(stage, capabilities):
    plan = stage.plan
    previous = package.read(stage.out.parent/'primary'/RECEIPT) if plan['name'] == 'light' else None
    params = dict(base=f"https://127.0.0.1:{plan['public_port']}/v1", key=stage.keys['alpha'],
                  other=stage.keys['beta'], sdk=plan['sdk_directory'], model='qwen3.8-flash-next')
    official = Path(plan[KEY]['official'])
    package.require(package.sha(official) == package.read(PROOF)['official_sha256'],
                    'official contract identity changed')
    row = exercise(stage.out, params, dict(os.environ, NODE_EXTRA_CA_CERTS=plan['tls']['certificate']),
                   official, previous)
    package.save(stage.out/RECEIPT, row)
    verify(stage.out, plan, capabilities)


def verify(phase, plan, capabilities):
    change, name = plan[KEY], plan['name']
    path = phase/'previous-profile.json'
    prior = profile_record.read(path)
    package.require(package.sha(path) == change['previous'] == reuse.UPLOAD_REVIEWED[name]
                    and change['release'] == plan['selected_sha256'] == package.read(RELEASE)['publication']['primary']
                    and prior['capacity'] == plan['capacity']
                    and package.same_json(dict(prior['capabilities'], protocol_source_sha256=change['protocol']),
                                          capabilities), 'issued profile or capabilities changed')
    previous = package.read(phase.parent/'primary'/RECEIPT) if name == 'light' else None
    check_result(package.read(phase/RECEIPT), previous)
    inherited = prior['business']['live']['evidence_sha256']
    package.require(COMMON <= inherited.keys() and 'chatkit-upload-handoff.json' in inherited,
                    'missing issued upload qualification')
    return {**{k:v for k,v in inherited.items() if k not in COMMON}, RECEIPT:package.sha(phase/RECEIPT)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('output', 'build', 'deployment', 'official'):
        parser.add_argument('--'+key, required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    package.require(output.is_relative_to(ROOT/'.q4t-work'), 'private evidence directory required')
    prepare(output, args.build.resolve(strict=True), args.deployment.resolve(strict=True),
            args.official.resolve(strict=True))

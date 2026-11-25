"""Qualify a browser-uploaded project document and its original conversation across service capacities."""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from tools.serve_recovery import profile_live_reuse as reuse, profile_record
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_responses.assistant_profiles import COMMON
from tools.serve_service import package

ROOT = lifecycle.ROOT
PROOF = ROOT / 'docs/evidence/serve-chatkit-20261125/upload.json'
RELEASE = ROOT / 'docs/evidence/serve-chatkit-20261125/feedback-delivery.json'
ACTOR = Path(__file__).with_name('chatkit_files_sdk.mjs').resolve()
VALIDATOR = ROOT / 'tools/serve_audit/sdk_wire_receipt.py'
RECEIPT = 'chatkit-upload-handoff.json'
KEY = 'chatkit_upload_handoff'


def prepare(out, build, deployment, official):
    proof = package.read(PROOF)
    package.require(proof['device']['status'] == 'PASS' and
                    package.sha(official) == proof['official_schema_sha256'],
                    'two-phase document HTTP verification required')
    sources = (ACTOR, VALIDATOR, Path(__file__).resolve(), Path(lifecycle.__file__), PROOF, RELEASE)
    lifecycle.prepare_candidate(out, build, deployment, RELEASE, reuse.FEEDBACK_REVIEWED, KEY, sources)
    for name in ('primary', 'light'):
        path = out / name / 'plan.json'
        plan = package.read(path)
        plan[KEY]['official'] = str(official)
        package.save(path, plan)


def stored_files(directory, row):
    hashes = {}
    for field, kind, prefix in [('file','file','file_'), ('thread','chatkit_thread','cthr_')]:
        item = row[field]
        package.require(re.fullmatch(prefix + r'[0-9a-f]{32}', item), 'invalid stored item ID')
        path, = directory.glob(f'*.{kind}.{item}.json')
        record = package.read(path)['record']
        package.require(record['id'] == item and record['kind'] == kind, 'invalid stored item')
        data = record['data']
        if field == 'file':
            package.require(data['file']['bytes'] == row['source_bytes'] and
                            data['chatkit_attachment']['id'] == item and
                            'upload_descriptor' not in data['chatkit_attachment'],
                            'published file metadata differs')
            # Opening a read lease CASes the file revision. All other fields,
            # including the chunk manifest, owner and expiry, remain immutable.
            hashes['file_revision'] = record['revision']
            hashes['file_identity'] = reuse.record_hash(
                {key: value for key, value in record.items() if key != 'revision'})
        else:
            package.require(data['items'][0]['attachments'][0]['id'] == row['file'],
                            'original thread lost its attachment')
        hashes[field] = package.sha(path)
    return hashes


def exercise(out, params, env, objects):
    from tools.serve_audit import sdk_wire_receipt
    previous = params.get('previous')
    before = stored_files(objects, previous) if previous else None
    with (out/'upload-sdk.stderr').open('xb') as err:
        result = subprocess.run(['node', ACTOR], input=json.dumps(dict(params, two_phase=True, retain=True)),
                                text=True, stdout=subprocess.PIPE, stderr=err, timeout=150, env=env)
    package.require(result.returncode == 0, 'two-phase upload or resumed SDK conversation failed')
    row = json.loads(result.stdout)
    observations = row.pop('wire', [])
    if observations:
        # The actor includes the body returned by the SDK listItems request.
        row['wire_receipts'] = sdk_wire_receipt.validate(package.read_openapi(Path(params['official'])),
            [dict(method=v['method'], path=v['path'], status=v['status'], value=v['body']) for v in observations])
    row['stored'] = stored_files(objects, row)
    if before:
        row['stored_before'] = before
    return row


def business(stage, capabilities):
    plan = stage.plan
    params = dict(base=f"https://127.0.0.1:{plan['public_port']}/v1", key=stage.keys['alpha'],
                  other=stage.keys['beta'], sdk=plan['sdk_directory'], model='qwen3.8-flash-next',
                  official=plan[KEY]['official'])
    package.require(package.sha(params['official']) == package.read(PROOF)['official_schema_sha256'],
                    'official schema identity changed')
    if plan['name'] == 'light':
        params['previous'] = package.read(stage.out.parent/'primary'/RECEIPT)
    objects = Path(plan['command'][plan['command'].index('--object-store-dir')+1])
    row = exercise(stage.out,params,dict(os.environ,NODE_EXTRA_CA_CERTS=plan['tls']['certificate']),objects)
    package.save(stage.out/RECEIPT,row)
    verify(stage.out,plan,capabilities)


def check_result(row, first, *, restored):
    document = ROOT/'docs/evidence/serve-scope-decision-20261120/decision.json'
    answer = str(package.read(document)['counts']['required_inference_or_application_service'])
    package.require(row['source_sha256'] == package.sha(document) and row['source_bytes'] == document.stat().st_size
                    and row['peer'] == 404 and row['independent_chat'] == '42', 'file contents or tenant access differ')
    if restored:
        package.require(row['upload_mode'] == 'restored' and row['stored_before'] == first['stored']
                        and row['stored']['file_identity'] == first['stored']['file_identity']
                        and row['stored']['file_revision'] > first['stored']['file_revision']
                        and row['items'] == first['items']+2
                        and row['answers'] == [answer] and all(row[k] == first[k] for k in ('workflow','thread','file')),
                        'original document or conversation did not survive handoff')
    else:
        expected = dict(pending_file=404,pending_model=404,replay=401,discarded=401,revoked_upload=401,
            short=400,wrong_mime=415,wrong_session=401,full_session_on_upload=401,upload_on_browser=401,origin_verified=True)
        package.require(row['upload_mode'] == 'two_phase' and row['reservation_checks'] == expected
                        and row['answers'] == [answer,answer] and row['items'] == 4 and len(row['wire_receipts']) == 1,
                        'original two-phase attachment, isolation or inference failed')


def verify(phase, plan, capabilities):
    prior = profile_record.read(phase/'previous-profile.json')
    change, name = plan[KEY], plan['name']
    expected = dict(prior['capabilities'], protocol_source_sha256=change['protocol'],
                    chatkit=reuse.UPLOAD_CHATKIT)
    package.require(package.sha(phase/'previous-profile.json') == change['previous']
                    == reuse.FEEDBACK_REVIEWED[name] and prior['capacity'] == plan['capacity']
                    and package.same_json(expected, capabilities) and plan['selected_sha256']
                    == change['release'] == package.read(RELEASE)['publication']['primary'],
                    'browser qualification requires the selected issued capacity')
    row = package.read(phase/RECEIPT)
    restored = name == 'light'
    first = package.read(phase.parent/'primary'/RECEIPT) if restored else row
    check_result(row, first, restored=restored)
    inherited = prior['business']['live']['evidence_sha256']
    package.require(COMMON <= inherited.keys() and 'chatkit-feedback-handoff.json' in inherited,
                    'missing issued browser feedback qualification')
    return {**{k:v for k,v in inherited.items() if k not in COMMON}, RECEIPT:package.sha(phase/RECEIPT)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('output','build','deployment','official'):
        parser.add_argument('--'+key, required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    package.require(output.is_relative_to(ROOT/'.q4t-work'), 'private evidence directory required')
    prepare(output, args.build.resolve(strict=True), args.deployment.resolve(strict=True),
            args.official.resolve(strict=True))

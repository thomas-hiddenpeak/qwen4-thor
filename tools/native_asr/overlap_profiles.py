"""Qualify two-speaker discovery and actual transcription at each issued capacity."""
import hashlib
import json
from pathlib import Path
import subprocess

from tools.native_asr import auxiliary_service
from tools.serve_recovery import profile_evidence, profile_live_reuse as reuse, profile_record
from tools.serve_service import package

ROOT = Path(__file__).resolve().parents[2]
KEY = 'overlap_transcription_handoff'
RECEIPT = 'overlap-transcription-handoff.json'
ACTOR = Path(__file__).with_name('overlap_profile_sdk.py')
PROOF = ROOT / 'docs/evidence/serve-transcription-discovery-20261128/device.json'
INPUTS = (Path(__file__), ACTOR, PROOF)


def prepare(out, build, deployment, python, sample):
    from tools.serve_responses import environment_lifecycle_profiles as lifecycle

    proof = package.read(PROOF)
    package.require(proof['engine'] == package.sha(build/'q4t') and
                    proof['ingress'] == package.sha(build/'q4t-ingress') and
                    proof['status'] == 'PASS' and proof['guard']['status'] == 'COMPLETED'
                    and package.sha(sample) == proof['source_sha256'] == profile_evidence.SEPARATOR_FILE_SHA,
                    'candidate engine, ingress or mixed speech differs from the HTTP short test')
    lifecycle.prepare_from_manifest(out, build, deployment, proof['predecessor_manifest'],
                                    reuse.STREAM_TEXT_REVIEWED, KEY, INPUTS)
    for name in ('primary', 'light'):
        path = out/name/'plan.json'
        plan = package.read(path)
        plan[KEY].update(python=str(python), audio=str(sample))
        package.save(path, plan)


def check(record, identity, config):
    package.require(record['sdk'] == '3.55.1' and record['method'] == 'POST'
                    and record['path'] == '/v1/audio/transcriptions' and record['status'] == 200
                    and isinstance(record['trace'], str) and bool(record['trace'])
                    and record['source_sha256'] == profile_evidence.SEPARATOR_FILE_SHA
                    and package.same_json(record['request'], profile_evidence.separator_request(identity)),
                    'wrong SDK operation, source or request')
    wire = record['wire'].encode('utf-8')
    package.require(hashlib.sha256(wire).hexdigest() == record['wire_sha256'],
                    'transcription response differs from its wire digest')
    result = auxiliary_service.separator_result(json.loads(wire), identity, config,
                                                record['source_sha256'])
    package.require(record['labels'] == result['labels'], 'SDK speaker labels differ from wire')
    return result


def business(stage):
    phase, plan = stage.out, stage.plan
    options = plan[KEY]
    previous = profile_record.read(phase/'previous-profile.json')
    request = profile_evidence.separator_request(previous['identity'])
    args = dict(base=f"https://127.0.0.1:{plan['public_port']}/v1", ca=plan['tls']['certificate'],
                key=stage.keys['alpha'], audio=options['audio'], request=request)
    stage.same_model()
    run = subprocess.run([options['python'], '-I', '-B', ACTOR], input=json.dumps(args).encode(),
                         capture_output=True, timeout=110)
    package.require(run.returncode == 0, 'overlap SDK failed on '+plan['name'])
    record = json.loads(run.stdout)
    status, raw = stage.http('/v1/config', label='overlap-health')
    package.require(status == 200, 'post-transcription health unavailable')
    check(record, previous['identity'], json.loads(raw))
    package.save(phase/RECEIPT, record)
    stage.same_model()


def verify(phase, plan, capabilities, current):
    proof, change = package.read(PROOF), plan[KEY]
    previous_file = phase/'previous-profile.json'
    previous = profile_record.read(previous_file)
    before = previous['capabilities']
    expected = dict(before, protocol_source_sha256=change['protocol'],
                    transcriptions=dict(before['transcriptions'], remaining_native_work=[]))
    package.require(before['transcriptions']['remaining_native_work'] == ['overlap_diarization']
                    and expected['transcriptions']['separator']['configured'] is True
                    and package.same_json(expected, capabilities) and previous['capacity'] == plan['capacity']
                    and package.sha(previous_file) == change['previous'] == reuse.STREAM_TEXT_REVIEWED[plan['name']]
                    and plan['selected_sha256'] == change['release'] == proof['predecessor_manifest']
                    and plan['binary_sha256'] == proof['engine'] and plan['ingress_sha256'] == proof['ingress']
                    and all(plan['sources'][str(p.relative_to(ROOT))] == package.sha(p) for p in INPUTS),
                    'previous capacity, discovery, evidence or qualified executables differ')
    config = package.read(phase/'overlap-health.wire')
    package.require(config['protocol_source_sha256'] == change['protocol'],
                    'health from another protocol')
    checked = package.read(phase/RECEIPT)
    check(checked, previous['identity'], config)
    if plan['name'] == 'light':
        first = package.read(phase.parent/'primary'/RECEIPT)
        package.require(checked['trace'] != first['trace'], 'capacity reused the primary request')
    evidence = previous['business']['live']['evidence_sha256']
    package.require(reuse.STREAM_TEXT_RECEIPT in evidence, 'missing preceding stream qualification')
    return {**{k:v for k,v in evidence.items() if k not in current},
            RECEIPT:package.sha(phase/RECEIPT), 'overlap-health.wire':package.sha(phase/'overlap-health.wire')}

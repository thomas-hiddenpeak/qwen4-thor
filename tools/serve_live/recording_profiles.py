"""Bind the earlier signed recording downloads to the same engine and capacity."""
from pathlib import Path

from tools.serve_recovery import profile_live_reuse as reuse, profile_record
from tools.serve_service import package

KEY = 'recording_download_handoff'
ROOT = Path(__file__).resolve().parents[2]
PROOF = ROOT / 'docs/evidence/serve-sdk-method-matrix-20261127/recording-device.json'
RELEASE = ROOT / 'docs/evidence/serve-inference-http-errors-20261127/delivery.json'
CLIENTS = {('python', '2.8.1'), ('python', '2.33.0'), ('python', '3.42.1'),
           ('python', '3.55.1'), ('node', '6.34.0'), ('node', '7.47.0')}


def check(proof, name, plan):
    release = package.read(RELEASE)['publication']
    wave = proof['real_recording']
    final = proof['final']
    observed = proof['profiles'][name]
    rows = observed['sdk']
    package.require(proof['engine'] == plan['binary_sha256'] and
                    proof['ingress'] == plan['ingress_sha256'] and
                    proof['capacity_source'] == release['publication_revision'] and
                    release['primary'] == plan[KEY]['release'] == plan['selected_sha256'] and
                    final['status'] == 'PASS' and final['default_unchanged'] is True and
                    final['signed_downloads'] == 12 and final['starts'] == 2 and
                    wave['original_status'] == 'PASS' and wave['original_store_unchanged'] is True,
                    'recording receipt differs from the engine, ingress or selected release')
    package.require(package.same_json(observed['capacity'], plan['capacity']) and
                    observed['foreign_download'] == 404 and observed['direct_401']['status'] == 401 and
                    observed['direct_401']['challenge'] == 'Bearer realm="q4t"' and
                    observed['shutdown_inflight'] == 0 and observed['shutdown_charged_bytes'] == 0 and
                    len(rows) == len(CLIENTS) and {(row['language'], row['sdk']) for row in rows} == CLIENTS and
                    len({row['trace'] for row in rows}) == len(rows) and
                    all(row['status'] == 200 and row['trace'] and row['bytes'] == wave['bytes'] and
                        row['sha256'] == wave['sha256'] for row in rows),
                    'recording client, capacity, owner isolation or byte identity differs')


def verify(phase, plan, capabilities, current):
    """Keep the new run's observations and the exact prior unaffected receipts."""
    prior_file = phase / 'previous-profile.json'
    previous = profile_record.read(prior_file)
    name = plan['name']
    digest = package.sha(PROOF)
    package.require(package.sha(prior_file) == reuse.NODE_SDK_REVIEWED[name] == plan[KEY]['previous'] and
                    plan['sources'][str(PROOF.relative_to(ROOT))] == digest and
                    previous['capacity'] == plan['capacity'] and
                    package.same_json(capabilities, dict(previous['capabilities'],
                                                         protocol_source_sha256=plan[KEY]['protocol'])),
                    'recording qualification changed a previous capacity or capability')
    check(package.read(PROOF), name, plan)
    receipts = previous['business']['live']['evidence_sha256']
    return {**{key: value for key, value in receipts.items() if key not in current},
            'recording-device.json': digest}

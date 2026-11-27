"""Qualify the native asset reader with project-document retrieval on both capacities."""
import math
from pathlib import Path

from tools.native_embedding import business_acceptance as embeddings
from tools.serve_recovery import profile_live_reuse as reuse, profile_record
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_service import package

ROOT = lifecycle.ROOT
CORPUS = ROOT/'docs/evidence/serve-native-embedding-business-20261111'
PROOF = ROOT/'docs/evidence/serve-asset-admission-20261127/device.json'
KEY = 'asset_reader_retrieval'
RECEIPT = 'asset-retrieval.json'
# Two original passages (optional nginx, native observation) and their queries.
INDICES = (2, 5, 7, 10)


def inputs():
    values = package.read(CORPUS/'default.request.json')['request']['input']
    return [values[i] for i in INDICES]


def check(response):
    rows = embeddings.vectors(response, 4, 1024)
    original = embeddings.vectors(package.read(CORPUS/'default.wire'), 11, 1024)
    def dot(left, right):
        return math.fsum(a*b for a, b in zip(left, right))
    comparisons = []
    for index, row in zip(INDICES, rows):
        reference = original[index]
        similarity = dot(row, reference)
        error = max(abs(a-b) for a, b in zip(row, reference))
        package.require(similarity >= .999 and error <= .005,
                        'vector differs from the accepted project retrieval')
        comparisons.append(dict(cosine=similarity, max_absolute_error=error))
    scores = [[dot(query, document) for document in rows[:2]] for query in rows[2:]]
    package.require(scores[0][0] > scores[0][1] and scores[1][1] > scores[1][0],
                    'project questions did not retrieve their own passages')
    return dict(model=embeddings.MODEL, dimensions=1024, vectors=comparisons,
                cosine_scores=scores, prompt_tokens=response['usage']['prompt_tokens'])


def prepare(out, build, deployment):
    proof = package.read(PROOF)
    package.require(proof['stage'] == 'native_reader_contention_on_primary'
                    and proof['engine'] == package.sha(build/'q4t')
                    and proof['environment']['status'] == 'PASS'
                    and proof['service_manifest'] == package.sha(deployment/'build/service/manifest.json'),
                    'candidate or release differs from the device test')
    files = [Path(__file__), PROOF, CORPUS/'default.request.json',
             CORPUS/'default.wire', CORPUS/'acceptance-final.json']
    package.require(package.read(files[-1])['status'] == 'ACCEPT_NATIVE_RETRIEVAL_BUSINESS',
                    'expected accepted native retrieval corpus')
    lifecycle.prepare_from_manifest(out, build, deployment, proof['service_manifest'],
                                    reuse.CURSOR_PAGE_REVIEWED, KEY, files)


def business(stage):
    status, response = stage.api('POST', '/v1/embeddings', timeout=45, body=dict(
        model=embeddings.MODEL, input=inputs(), encoding_format='base64', dimensions=1024))
    package.require(status == 200, f'native project retrieval returned HTTP {status}')
    received = stage.out/'asset-retrieval-response.json'
    package.save(received, response)
    package.save(stage.out/RECEIPT, dict(check(response), response_sha256=package.sha(received)))


def verify(phase, plan, capabilities, current):
    old = phase/'previous-profile.json'
    previous = profile_record.read(old)
    change = plan[KEY]
    proof = package.read(PROOF)
    package.require(package.sha(old) == change['previous'] == reuse.CURSOR_PAGE_REVIEWED[plan['name']]
                    and proof['engine'] == plan['binary_sha256']
                    and proof['service_manifest'] == change['release'] == plan['selected_sha256']
                    and plan['sources'][str(PROOF.relative_to(ROOT))] == package.sha(PROOF)
                    and previous['capacity'] == plan['capacity']
                    and package.same_json(capabilities, dict(previous['capabilities'],
                                                   protocol_source_sha256=change['protocol'])),
                    'retrieval release, source or capacity changed')
    received = phase/'asset-retrieval-response.json'
    result = package.read(phase/RECEIPT)
    expected = dict(check(package.read(received)), response_sha256=package.sha(received))
    package.require(package.same_json(result, expected),
                    'retrieval observation differs from the HTTP response')
    if plan['name'] == 'light':
        primary = package.read(phase.parent/'primary'/RECEIPT)
        package.require(primary['prompt_tokens'] == result['prompt_tokens'],
                        'identical project inputs consumed different token counts')
    inherited = previous['business']['live']['evidence_sha256']
    package.require(reuse.CURSOR_PAGE_RECEIPT in inherited, 'issued cursor history missing')
    return {**{k:v for k, v in inherited.items() if k not in current},
            RECEIPT:package.sha(phase/RECEIPT)}

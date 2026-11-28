"""Qualify repeated Responses selectors against one stored result per capacity."""
import math
from pathlib import Path
from urllib.parse import urlencode

from tools.serve_recovery import profile_live_reuse as history, profile_record
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_service import package

ROOT = Path(__file__).resolve().parents[2]
KEY = 'responses_include_projection'
RECEIPT = 'responses-include-projection.json'
PROOF = ROOT/'docs/evidence/serve-response-include-projection-20261128/device.json'
INPUTS = (Path(__file__), PROOF)
SELECTOR = 'message.output_text.logprobs'


def prepare(out, build, deployment):
    proof = package.read(PROOF)
    package.require(proof['status'] == 'PASS' and proof['runtime']['ports_free']
                    and proof['candidate'] == package.sha(build/'q4t')
                    and proof['ingress'] == package.sha(build/'q4t-ingress'),
                    'projection profiles require the short-tested engine and gateway')
    lifecycle.prepare_from_manifest(out, build, deployment, proof['primary'],
                                    history.PRODUCER_REVIEWED, KEY, INPUTS)
    for name in ('primary', 'light'):
        path = out/name/'plan.json'
        plan = package.read(path)
        plan['limits']['projected_responses'] = 1
        package.save(path, plan)


def projection(response):
    parts = [part for item in response['output'] if item['type'] == 'message'
             for part in item['content'] if part['type'] == 'output_text']
    package.require(len(parts) == 1, 'expected one projected answer')
    return parts[0]['text'], parts[0].get('logprobs')


def check(row, revision):
    created = row['created']
    ident = created['id']
    text, tokens = projection(created)
    package.require(ident.startswith('resp_') and text.strip() == str(revision)
                    and isinstance(tokens, list) and bool(tokens)
                    and ''.join(t['token'] for t in tokens) == text,
                    'projection lost the publication number or token sequence')
    for token in tokens:
        score = token['logprob']
        package.require(type(score) in (float, int) and math.isfinite(score) and score <= 0
                        and bytes(token['bytes']) == token['token'].encode()
                        and len(token['top_logprobs']) == 1,
                        'invalid token probability or byte sequence')
    for view in (created, row['single'], row['repeated']):
        package.require(view['status'] == 'completed' and view['id'] == ident
                        and package.same_json(projection(view), (text, tokens)),
                        'stored projection differs from the completed response')
    package.require(row['peer_status'] == 404, 'another client retrieved the stored result')


def business(stage, capabilities):
    revision = package.read(PROOF)['revision']
    options = [SELECTOR] * 12
    stage.same_model()
    stage.budget('projected_responses')
    status, created = stage.api('POST', '/v1/responses', timeout=75, body=dict(
        model=capabilities['model'], input=f'The project publication receipt records revision {revision}. '
        'Reply with only the revision number.', temperature=0, reasoning=dict(effort='none'),
        store=True, include=options, top_logprobs=1, max_output_tokens=32))
    package.require(status == 200, f'projection create returned HTTP {status}')
    path = '/v1/responses/' + created['id'] + '?'

    def fetch(selectors, client='alpha'):
        return stage.api('GET', path + urlencode([('include[]', s) for s in selectors]), client=client)

    single_status, single = fetch(options[:1])
    repeated_status, repeated = fetch(options)
    peer_status, _ = fetch(options, 'beta')
    package.require(single_status == repeated_status == 200, 'owner could not read the response')
    row = dict(created=created, single=single, repeated=repeated, peer_status=peer_status)
    package.save(stage.out/RECEIPT, row)
    check(row, revision)
    stage.same_model()


def verify(phase, plan, capabilities, current):
    proof = package.read(PROOF)
    change = plan[KEY]
    previous_file = phase/'previous-profile.json'
    previous = profile_record.read(previous_file)
    expected = dict(previous['capabilities'], protocol_source_sha256=change['protocol'])
    package.require(plan['limits']['projected_responses'] == 1
                    and package.read(phase/'result.json')['counts']['projected_responses'] == 1
                    and package.sha(previous_file) == change['previous'] == history.PRODUCER_REVIEWED[plan['name']]
                    and plan['selected_sha256'] == change['release'] == proof['primary']
                    and plan['binary_sha256'] == proof['candidate'] and plan['ingress_sha256'] == proof['ingress']
                    and package.same_json(expected, capabilities)
                    and package.same_json(previous['capacity'], plan['capacity'])
                    and all(plan['sources'][str(p.relative_to(ROOT))] == package.sha(p) for p in INPUTS),
                    'projection run differs from the reviewed profile, capacity, binary or source')
    receipt = phase/RECEIPT
    row = package.read(receipt)
    check(row, proof['revision'])
    if plan['name'] == 'light':
        primary = package.read(phase.parent/'primary'/RECEIPT)
        package.require(primary['created']['id'] != row['created']['id'], 'reused the primary response')
    evidence = previous['business']['live']['evidence_sha256']
    package.require(history.PRODUCER_RECEIPT in evidence, 'missing issued producer evidence')
    return {**{k: v for k, v in evidence.items() if k not in current},
            RECEIPT: package.sha(receipt)}

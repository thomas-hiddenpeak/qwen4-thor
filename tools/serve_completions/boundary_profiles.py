"""Qualify the explicit completion candidate limit on both registered capacities."""
from pathlib import Path

from tools.serve_recovery import profile_live_reuse as history, profile_record
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_service import package

ROOT = Path(__file__).resolve().parents[2]
KEY = 'completion_candidate_limit'
RECEIPT = 'completion-candidate-limit.json'
PROOF = ROOT / 'docs/evidence/serve-completion-best-of-20261128/device.json'
INPUTS = (Path(__file__), PROOF)


def prepare(out, build, deployment):
    proof = package.read(PROOF)
    package.require(proof['status'] == 'PASS' and proof['runtime']['unchanged_default']
                    and proof['candidate'] == package.sha(build/'q4t')
                    and proof['gateway'] == package.sha(build/'q4t-ingress'),
                    'short-tested completion engine or ingress differs')
    lifecycle.prepare_from_manifest(out, build, deployment, proof['installed_manifest'],
                                    history.PROJECTION_REVIEWED, KEY, INPUTS)
    for name in ('primary', 'light'):
        path = out/name/'plan.json'
        plan = package.read(path)
        plan['limits']['completion_limits'] = 3
        package.save(path, plan)


def check(row, prompt):
    rejected, maximum, independent = (row[key] for key in ('rejected', 'maximum', 'independent'))
    error = rejected['response'].get('error', {})
    package.require(rejected['status'] == 400 and error.get('param') == 'best_of'
                    and error.get('code') == 'invalid_request', 'explicit candidate limit was not enforced')
    for call, count in [(maximum, 1), (independent, 21)]:
        response = call['response']
        package.require(call['status'] == 200 and response['object'] == 'text_completion'
                        and response['usage']['prompt_tokens'] > 0 and response['usage']['completion_tokens'] == 0
                        and [c['index'] for c in response['choices']] == list(range(count))
                        and all(c['text'] == prompt and c['finish_reason'] == 'length' for c in response['choices']),
                        'completion count, echo or zero-output usage differs')
    package.require(maximum['response']['id'] != independent['response']['id'],
                    'completion inputs reused a response ID')


def business(stage, capabilities):
    stage.same_model()
    prompt = 'q4t completion limits ' + stage.plan['name']
    row = {}
    for label, options in [('rejected', {'best_of': 21}), ('maximum', {'best_of': 20}),
                           ('independent', {'n': 21})]:
        stage.budget('completion_limits')
        body = dict(model=capabilities['model'], prompt=prompt, echo=True, max_tokens=0, **options)
        status, response = stage.api('POST', '/v1/completions', timeout=75, body=body)
        row[label] = dict(status=status, response=response)
    package.save(stage.out/RECEIPT, row)
    check(row, prompt)
    stage.same_model()


def verify(phase, plan, capabilities, current):
    proof = package.read(PROOF)
    change = plan[KEY]
    name = plan['name']
    previous_file = phase/'previous-profile.json'
    previous = profile_record.read(previous_file)
    expected = dict(previous['capabilities'], protocol_source_sha256=change['protocol'])
    package.require(plan['limits']['completion_limits'] == 3
                    and package.read(phase/'result.json')['counts']['completion_limits'] == 3
                    and package.sha(previous_file) == change['previous'] == history.PROJECTION_REVIEWED[name]
                    and plan['selected_sha256'] == change['release'] == proof['installed_manifest']
                    and plan['binary_sha256'] == proof['candidate'] and plan['ingress_sha256'] == proof['gateway']
                    and package.same_json(expected, capabilities)
                    and package.same_json(previous['capacity'], plan['capacity'])
                    and all(plan['sources'][str(p.relative_to(ROOT))] == package.sha(p) for p in INPUTS),
                    'completion run differs from the reviewed capacity, engine, gateway or source')
    receipt = phase/RECEIPT
    check(package.read(receipt), 'q4t completion limits ' + name)
    evidence = previous['business']['live']['evidence_sha256']
    package.require(history.PROJECTION_RECEIPT in evidence, 'missing issued Responses projection evidence')
    return {**{k:v for k,v in evidence.items() if k not in current}, RECEIPT: package.sha(receipt)}

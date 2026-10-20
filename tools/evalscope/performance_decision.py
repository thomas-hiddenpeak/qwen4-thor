"""Check an audited decision ledger; never infer statistics or run GPU work.

Inputs are reviewer assertions with references, not a substitute for evidence
validation. This tool neither grants deployment permission nor verifies a CI.
"""
import argparse
import json
from pathlib import Path

CHOICES = {
    'route': {'engineering', 'statistical'},
    'validity': {'valid', 'env_abort', 'unverified'},
    'contracts': {'pass', 'fail', 'incomplete'},
    'measurement': {'complete', 'incomplete', 'not_run'},
    'regression': {'confirmed', 'not_established'},
    'statistics': {'noninferior', 'regression', 'inconclusive', 'not_requested'},
    'engineering_decision': {'accepted', 'rejected', 'pending'},
}


def assess(ledger):
    for key, allowed in CHOICES.items():
        if ledger.get(key) not in allowed:
            raise ValueError('invalid or missing ' + key)
    for key in ['candidate_sha256', 'plan_reference', 'evidence_reference',
                'route_reference']:
        if not isinstance(ledger.get(key), str) or not ledger[key].strip():
            raise ValueError('missing ' + key)
    digest = ledger['candidate_sha256']
    if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        raise ValueError('candidate identity must be a full lowercase SHA256')
    for key in ['request_budget', 'requests_used']:
        if type(ledger.get(key)) is not int or ledger[key] < 0:
            raise ValueError('invalid ' + key)
    for condition, reference in [
            (ledger['engineering_decision'] != 'pending', 'decision_reference'),
            (ledger['regression'] == 'confirmed', 'regression_reference'),
            (ledger['statistics'] in {'noninferior', 'regression'},
             'statistical_analysis_reference')]:
        if condition and not str(ledger.get(reference, '') or '').strip():
            raise ValueError('missing ' + reference)
    remaining = ledger['request_budget'] - ledger['requests_used']
    result = dict(candidate_sha256=digest, statistics=ledger['statistics'],
                  remaining_request_budget=max(0, remaining),
                  automatic_resampling=False, deployment_authorized=False,
                  evidence_validation='external audit required')

    def finish(status, action):
        return dict(result, status=status, next_action=action)

    if ledger['validity'] != 'valid':
        return finish('ENV_ABORT' if ledger['validity'] == 'env_abort'
                      else 'EVIDENCE_UNVERIFIED',
                      'Preserve evidence; resolve validity before any acceptance.')
    if ledger['contracts'] == 'fail':
        return finish('CONTRACT_FAILURE', 'Fix the concrete failure; no acceptance.')
    if ledger['regression'] == 'confirmed' or ledger['statistics'] == 'regression':
        return finish('REGRESSION_CONFIRMED', 'Reject or fix; gains elsewhere do not offset it.')
    if remaining < 0:
        return finish('BUDGET_EXCEEDED', 'Audit the protocol violation; do not silently accept.')
    if ledger['contracts'] != 'pass':
        return finish('CONTRACTS_INCOMPLETE', 'Finish the affected contract checks within scope.')
    if ledger['engineering_decision'] == 'rejected':
        return finish('REJECTED', 'Keep the baseline; do not resample to reverse this decision.')
    if ledger['measurement'] != 'complete':
        return finish('MEASUREMENT_INCOMPLETE',
                      'Execute only the remaining authorized frozen measurement.' if remaining
                      else 'Keep baseline; name the missing measurement and propose a bounded plan.')
    if ledger['route'] == 'statistical':
        if ledger['statistics'] == 'noninferior':
            return finish('STATISTICAL_CRITERION_MET', 'Follow the separate release authorization.')
        return finish('STATISTICAL_INCONCLUSIVE',
                      'Close this statistical stage as inconclusive; keep baseline unless a separate engineering route is accepted.')
    if ledger['engineering_decision'] == 'accepted':
        return finish('ENGINEERING_ACCEPTED',
                      'Reuse this identity-specific decision; follow its deployment scope without asking the same acceptance again.')
    return finish('ENGINEERING_DECISION_PENDING',
                  'Present one complete evidence-based recommendation; no automatic calibration or repeat confirmation after acceptance.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('ledger', type=Path)
    args = parser.parse_args()
    try:
        result = assess(json.loads(args.ledger.read_text()))
    except (ValueError, KeyError, TypeError) as error:
        parser.error(str(error))
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()

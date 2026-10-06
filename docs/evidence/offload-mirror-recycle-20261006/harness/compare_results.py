"""Compare fixed mirror-recycle HTTP decisions; no raw-log/resource rereads."""
import argparse
import math
from pathlib import Path
import re
import time

from recycle_common import R, frozen, read, save, sha

HISTORY_PAIRS = (('h01-off', 'h02-on'), ('h04-off', 'h03-on'))
FIELDS = ('text', 'prompt_sha256', 'actual_input', 'actual_output', 'finish')
COUNTERS = ('plans', 'attempts', 'preferred', 'changed', 'fallback',
            'unavailable', 'published')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def compare_three(before, after, label):
    require(len(before) == len(after) == 3, label + ': need all three repeats')
    require(all(positive(row[key]) for rows in (before, after) for row in rows
                for key in ('ttft', 'decode_tps')), label + ': invalid timing')
    old = {'first_ttft': before[0]['ttft'],
           'later_max_ttft': max(row['ttft'] for row in before[1:]),
           'minimum_decode_tps': min(row['decode_tps'] for row in before)}
    new = {'first_ttft': after[0]['ttft'],
           'later_max_ttft': max(row['ttft'] for row in after[1:]),
           'minimum_decode_tps': min(row['decode_tps'] for row in after)}
    checks = {'first_ttft': new['first_ttft'] <= old['first_ttft'],
              'later_max_ttft': new['later_max_ttft'] <= old['later_max_ttft'],
              'minimum_decode_tps': new['minimum_decode_tps'] >=
                                    old['minimum_decode_tps']}
    return {'label': label, 'passed': all(checks.values()), 'checks': checks,
            'failed_checks': [key for key, passed in checks.items() if not passed],
            'baseline': before, 'candidate': after,
            'baseline_gate_values': old, 'candidate_gate_values': new,
            'candidate_relative_change_percent': {
                key: 100 * (new[key] / old[key] - 1) for key in old},
            'relative_change_meaning': 'Positive TTFT change is slower; positive '
                'decode TPS change is faster. No epsilon or significance claim.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('scope', choices=('history', 'all'))
    parser.add_argument('--plan-sha256', required=True)
    args = parser.parse_args()
    output = R / ('history-decision.json' if args.scope == 'history' else
                  'performance-decision.json')
    require(not output.exists(), 'first comparison record already exists')
    sources, provenance = {}, {}
    report = {'schema': 1, 'scope': args.scope, 'passed': False,
              'decision': 'CONTRACT_FAILURE', 'evidence_contracts_passed': False,
              'plan_sha256': args.plan_sha256, 'started_t': time.time(),
              'source_sha256': sources, 'group_decision_provenance': provenance,
              'failure': None, 'performance_acceptance': False,
              'resource_acceptance': False, 'default_enable_allowed': False,
              'whole_physical_RAM_54GB': 'INDETERMINATE',
              'raw_log_reads': 0, 'raw_resource_reads': 0}
    returncode = 0

    def bound(path):
        path = Path(path).resolve()
        require(path.suffix == '.json', 'comparison consumes JSON decisions only')
        digest = sha(path)
        require(str(path) not in sources or sources[str(path)] == digest,
                'decision source changed during comparison')
        sources[str(path)] = digest
        return read(path)

    try:
        plan = frozen(args.plan_sha256)
        report.update(runtime_source_commit=plan['runtime_source_commit'],
                      runtime_binary_sha256=plan['runtime_binary_sha256'])
        phase = bound(plan['protocol_plan_path'])
        require(phase['groups'] == plan['groups'] and
                phase['service_count'] == plan['service_count'] == 17 and
                phase['http_count'] == plan['http_count'] == 131 and
                phase['history']['comparison_blocks'] ==
                [list(pair) for pair in HISTORY_PAIRS] and
                phase['matrix']['repeats'] == 3 and
                phase['history']['rounds'] == 3 and phase['epsilon'] is None,
                'comparison scope differs from frozen protocol')
        groups = {group['id']: group for group in phase['groups']}
        require(len(groups) == 17 and list(groups) == plan['group_ids'],
                'duplicate or reordered frozen groups')
        selected = ([name for pair in HISTORY_PAIRS for name in pair]
                    if args.scope == 'history' else plan['group_ids'])
        records = {}
        for name in selected:
            group = groups[name]
            record = bound(R / (name + '-decision.json'))
            require(record['passed'] is True and record['failure'] is None and
                    record['plan_sha256'] == args.plan_sha256 and
                    record['runtime_source_commit'] == plan['runtime_source_commit'] and
                    record['runtime_binary_sha256'] == plan['runtime_binary_sha256'] and
                    record['group_id'] == name and record['kind'] == group['kind'] and
                    record['mirror_gpu_recycle'] == group['arm'] and
                    record['arm'] == ('B' if group['arm'] else 'A') and
                    record['performance_acceptance'] is False and
                    record['status'] == ('PASS_FIXED_HTTP_11' if
                        group['kind'] == 'quality' else 'PASS_GROUP_CONTRACTS') and
                    record['request_count'] == len(record['requests']) ==
                    group['requests'], name + ': failed or misidentified HTTP decision')
            require(positive(record['ended_t']) and positive(record['recorded_t']) and
                    record['ended_t'] <= record['recorded_t'] <= report['started_t'],
                    name + ': HTTP decision is not terminal')
            # Bind the already-audited source map through its decision-file SHA.
            # Its transitive raw logs/resources are deliberately not consumed here.
            require(record['source_sha256'] and all(Path(path).is_absolute() and
                    re.fullmatch(r'[0-9a-f]{64}', digest) for path, digest in
                    record['source_sha256'].items()), name + ': malformed source ledger')
            provenance[name] = record['source_sha256']
            recycle = record['recycle']
            totals = recycle['totals']
            require(recycle['passed'] is True and recycle['enabled'] == group['arm'] and
                    recycle['request_count'] == group['requests'] and
                    set(totals) == set(COUNTERS) and
                    all(type(value) is int and value >= 0 for value in totals.values()) and
                    totals['attempts'] == totals['preferred'] + totals['fallback'] +
                    totals['unavailable'] and totals['changed'] <= totals['preferred'] and
                    totals['published'] <= totals['preferred'] and
                    (group['arm'] == 1 or not any(totals.values())),
                    name + ': recycle decision counters differ')
            if group['kind'] != 'quality':
                require(len(record['metrics']) == group['requests'],
                        name + ': missing timing repeat')
                for index, (metric, request) in enumerate(
                        zip(record['metrics'], record['requests'])):
                    if group['kind'] == 'history':
                        position = index % 7 + 1
                        round_index = index // 7 + 1
                        length = phase['history']['sequence'][position - 1]
                        case = f'sequence-r{round_index:02d}-p{position:02d}-context-{length}'
                        require(metric['round'] == round_index and
                                metric['position'] == position and metric['repeat'] == 1,
                                name + ': history rounds or positions changed')
                        expected_output = 256
                    else:
                        length, expected_output = group['input_tokens'], group['output_tokens']
                        case = 'context-' + str(length)
                        require(metric['repeat'] == index + 1,
                                name + ': matrix repeats reordered')
                    require(metric['case'] == case and metric['input_tokens'] == length and
                            request['actual_input'] == length and
                            request['actual_output'] == expected_output and
                            request['finish'] == ['length'] and
                            all(positive(metric[key]) for key in ('ttft', 'decode_tps')),
                            name + ': fixed request or timing differs')
            records[name] = record

        def compare_outputs(off, on):
            old, new = records[off]['requests'], records[on]['requests']
            require(len(old) == len(new) and all(
                all(a[key] == b[key] for key in FIELDS) for a, b in zip(old, new)),
                off + '/' + on + ': exact input/output changed')

        report['ended_t'] = max(record['ended_t'] for record in records.values())
        changed = {name: record['recycle']['totals']['changed']
                   for name, record in records.items()}
        report['selection_change'] = {'changed_by_group': changed,
            'total_changed': sum(changed.values()),
            'observed': sum(changed.values()) > 0,
            'meaning': 'Actual different ring target; no avoided-read or speed inference.'}
        if args.scope == 'history':
            for name in ('h02-on', 'h03-on', 'h04-off'):
                compare_outputs('h01-off', name)
            report['four_group_exact_outputs_equal'] = True
            report['rounds_retained_per_position'] = 3
            blocks = []
            for off, on in HISTORY_PAIRS:
                compare_outputs(off, on)
                positions = []
                for position in range(1, 8):
                    before = [row for row in records[off]['metrics']
                              if row['position'] == position]
                    after = [row for row in records[on]['metrics']
                             if row['position'] == position]
                    row = compare_three(before, after, f'{off}/{on}:p{position}')
                    row.update(position=position,
                        input_tokens=phase['history']['sequence'][position - 1])
                    positions.append(row)
                blocks.append({'baseline_group': off, 'candidate_group': on,
                    'positions': positions, 'passed': all(row['passed'] for row in positions)})
            report.update(blocks=blocks, request_count=84,
                passed=all(block['passed'] for block in blocks),
                failed_checks=[row['label'] + ':' + key for block in blocks
                    for row in block['positions'] for key in row['failed_checks']],
                matrix_coverage_allowed=True,
                matrix_coverage_reason='Complete the frozen coverage even if history vetoes '
                    'acceptance. No retries, epsilon, extra matrix or second candidate.')
        else:
            history = bound(R / 'history-decision.json')
            require(history['evidence_contracts_passed'] is True and
                    type(history['passed']) is bool and history['matrix_coverage_allowed'] and
                    history['decision'] == ('PASS_SCREENING' if history['passed'] else 'NO_GO') and
                    history['plan_sha256'] == args.plan_sha256 and
                    history['runtime_source_commit'] == plan['runtime_source_commit'] and
                    history['runtime_binary_sha256'] == plan['runtime_binary_sha256'],
                    'history veto record missing or changed identity')
            for off, on in HISTORY_PAIRS:
                for name in (off, on):
                    path = str((R / (name + '-decision.json')).resolve())
                    require(history['source_sha256'][path] == sources[path],
                            'history group changed after the frozen veto')
            tiers = []
            for length in phase['matrix']['lengths']:
                matches = [group for group in phase['groups'] if
                           group['kind'] == 'matrix' and group['input_tokens'] == length]
                require(len(matches) == 2 and {group['arm'] for group in matches} == {0, 1},
                        'matrix tier requires exactly one service per arm')
                arms = {group['arm']: group['id'] for group in matches}
                off, on = arms[0], arms[1]
                compare_outputs(off, on)
                row = compare_three(records[off]['metrics'], records[on]['metrics'],
                                    f'context-{length}')
                row.update(input_tokens=length, baseline_group=off, candidate_group=on)
                tiers.append(row)
            matrix_count = sum(group['requests'] for group in phase['groups']
                               if group['kind'] == 'matrix')
            require(len(tiers) == 6 and matrix_count == 36 and
                    sum(record['request_count'] for record in records.values()) == 131,
                    'full frozen coverage is incomplete')
            gates_passed = history['passed'] and all(row['passed'] for row in tiers)
            activation = report['selection_change']['observed']
            failed = list(history['failed_checks']) + [row['label'] + ':' + key
                     for row in tiers for key in row['failed_checks']]
            if not activation:
                failed.append('actual_selection_change_not_observed')
            report.update(tiers=tiers, history_passed=history['passed'],
                history_veto_retained=not history['passed'],
                history_failed_checks=history['failed_checks'], matrix_request_count=36,
                request_count=131, service_count=17, failed_checks=failed,
                all_speed_gates_passed=gates_passed,
                actual_intervention_gate_passed=activation,
                passed=gates_passed and activation,
                experiment_performance_passed=gates_passed and activation,
                full_frozen_coverage_completed=True)
        report.update(evidence_contracts_passed=True,
            decision='PASS_SCREENING' if report['passed'] else 'NO_GO',
            interpretation_limits=[
                'All frozen observations retained; no outlier removal or retry.',
                'History ABBA and alternating matrix order do not remove all drift.',
                'Observed extrema gates are not confidence intervals or tail claims.',
                'Performance screening does not certify resources, physical54GB, '
                'all-model precision, or default enablement.'])
    except BaseException as error:
        report.update(failure=type(error).__name__ + ': ' + str(error))
        returncode = 1
    finally:
        report.setdefault('ended_t', time.time())
        report['recorded_t'] = time.time()
        sources[str(Path(__file__).resolve())] = sha(__file__)
        save(output, report)
        print({key: report.get(key) for key in
               ('scope', 'decision', 'passed', 'evidence_contracts_passed',
                'request_count', 'failure')}, flush=True)
    return returncode


if __name__ == '__main__':
    raise SystemExit(main())

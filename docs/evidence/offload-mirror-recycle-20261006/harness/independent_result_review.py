"""Bounded independent review of this phase's decisions, never inference.

Does not import the implementation comparator, runner, model, or raw resource
reader. Execute only after root says the complete batch or early stop is final.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import time

R = Path(__file__).resolve().parent
IDENTITY = ('runtime_source_commit', 'runtime_binary_sha256')
COUNTERS = ('plans', 'attempts', 'preferred', 'changed', 'fallback',
            'unavailable', 'published')
OUTPUT_FIELDS = ('text', 'prompt_sha256', 'actual_input', 'actual_output', 'finish')
HISTORY = (16385, 8192, 8193, 1024, 45056, 4096, 8192)
PAIRS = (('h01-off', 'h02-on'), ('h04-off', 'h03-on'))
LENGTHS = (1024, 4096, 8192, 45056, 204800, 261887)
MAX_FILE = 32 << 20
MAX_TOTAL = 128 << 20


def require(condition, message):
    if not condition:
        raise ValueError(message)


def positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


class Reader:
    def __init__(self):
        self.values, self.sources, self.signatures = {}, {}, {}
        self.bytes = 0
        self.external = set()

    def read(self, path, expected=None):
        path = Path(path).resolve()
        require(path.suffix == '.json' and
                (path.is_relative_to(R) or str(path) in self.external),
                'review only permits this phase JSON and two bound oracles')
        key = str(path)
        if key not in self.values:
            before = path.stat()
            require(before.st_size <= MAX_FILE and
                    self.bytes + before.st_size <= MAX_TOTAL,
                    'bounded review metadata limit exceeded')
            raw = path.read_bytes()
            after = path.stat()
            signature = lambda stat: (stat.st_dev, stat.st_ino, stat.st_size,
                                      stat.st_mtime_ns)
            require(signature(before) == signature(after), 'source changed during read')
            self.sources[key] = hashlib.sha256(raw).hexdigest()
            self.signatures[key] = signature(after)
            self.values[key] = json.loads(raw)
            self.bytes += len(raw)
        require(expected is None or self.sources[key] == expected,
                'bound JSON SHA mismatch: ' + key)
        return self.values[key]

    def stable(self):
        for key, signature in self.signatures.items():
            stat = Path(key).stat()
            require((stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns) == signature,
                    'source changed after read: ' + key)


def identity(record, plan, digest, label):
    require(record.get('plan_sha256') == digest and
            all(record.get(key) == plan[key] for key in IDENTITY),
            label + ': decision identity differs')


def counters(record, group):
    evidence = record['recycle']
    requests = record['requests']
    rows = evidence['requests']
    require(evidence['passed'] is True and evidence['enabled'] == group['arm'] and
            evidence['request_count'] == len(rows) == len(requests),
            group['id'] + ': recycle row count or policy differs')
    totals = dict.fromkeys(COUNTERS, 0)
    for request, row in zip(requests, rows):
        require(row['response_id'] == request['response_id'],
                group['id'] + ': recycle row request identity differs')
        values = {key: row[key] for key in COUNTERS}
        require(all(type(value) is int and 0 <= value < 2**64
                    for value in values.values()), 'invalid per-request counter')
        require(values['attempts'] == sum(values[key] for key in
                    ('preferred', 'fallback', 'unavailable')),
                'per-request attempt partition differs')
        require(values['changed'] <= values['preferred'] and
                values['published'] <= values['preferred'] and
                values['plans'] <= 48 * (request['actual_output'] - 1) and
                (values['plans'] > 0 or not any(values.values())),
                'per-request subset or true-decode bound differs')
        require(group['arm'] == 1 or not any(values.values()),
                'OFF contains candidate work')
        for key in COUNTERS:
            totals[key] += values[key]
    require(evidence['totals'] == totals and
            evidence['selection_change_observed'] is (totals['changed'] > 0),
            'aggregate recycle counters differ from independent sum')
    return totals


def rows_equal(left, right, label):
    require(len(left) == len(right) and all(
        all(a[key] == b[key] for key in OUTPUT_FIELDS) for a, b in zip(left, right)),
        label + ': prompt/output/usage/finish differs')


def gate(before, after, label):
    require(len(before) == len(after) == 3 and
            all(positive(row[key]) for rows in (before, after) for row in rows
                for key in ('ttft', 'decode_tps')), label + ': three timings required')
    # Independent direct extrema, with no epsilon, rounding or outlier removal.
    old = (before[0]['ttft'], max(before[1]['ttft'], before[2]['ttft']),
           min(row['decode_tps'] for row in before))
    new = (after[0]['ttft'], max(after[1]['ttft'], after[2]['ttft']),
           min(row['decode_tps'] for row in after))
    names = ('first_ttft', 'later_max_ttft', 'minimum_decode_tps')
    checks = dict(zip(names, (new[0] <= old[0], new[1] <= old[1], new[2] >= old[2])))
    return dict(label=label, checks=checks, passed=all(checks.values()),
                baseline_gate_values=dict(zip(names, old)),
                candidate_gate_values=dict(zip(names, new)),
                failed_checks=[key for key in names if not checks[key]])


def compare_saved_gate(actual, expected):
    for key in ('label', 'checks', 'passed', 'baseline_gate_values',
                'candidate_gate_values', 'failed_checks'):
        require(actual[key] == expected[key], expected['label'] + ': saved gate differs')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan-sha256', required=True)
    parser.add_argument('--mode', required=True, choices=('complete', 'early'))
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    require(output.parent == R and output.suffix == '.json' and not output.exists(),
            'review output must be an exclusive new root-level JSON')
    reader = Reader()
    report = dict(schema=1, passed=False, status='REVIEW_NOT_COMPLETE',
        mode=args.mode, started_t=time.time(), failure=None,
        model_runs=0, http_requests_created=0, tests_executed=0,
        raw_log_reads=0, raw_resource_reads=0, weight_payload_reads=0,
        default_enable_allowed=False, whole_physical_RAM_54GB='INDETERMINATE',
        performance_acceptance=False, independent_arithmetic=True)
    exit_code = 1
    try:
        plan = reader.read(R / 'execution-plan.json', args.plan_sha256)
        phase = reader.read(plan['protocol_plan_path'], plan['protocol_plan_sha256'])
        scope = reader.read(R / 'scope-plan.json', plan['scope_sha256'])
        require(plan['groups'] == phase['groups'] and plan['service_count'] ==
                phase['service_count'] == 17 and plan['http_count'] == phase['http_count'] == 131,
                'frozen service/request scope differs')
        require(phase['default'] == 0 and phase['epsilon'] is None and
                phase['whole_physical_RAM_54GB'] == 'INDETERMINATE' and
                scope['single_candidate']['env']['unset'] == '0' and
                list(HISTORY) == phase['history']['sequence'] and
                list(LENGTHS) == phase['matrix']['lengths'] and
                phase['history']['comparison_blocks'] == [list(pair) for pair in PAIRS],
                'default, unknown resource status or acceptance gates changed')
        groups = {group['id']: group for group in phase['groups']}
        require(list(groups) == plan['group_ids'], 'group order differs')
        admission = reader.read(R / 'execution-admission.json')
        require(admission['execution_admitted'] and
                admission['execution_plan_sha256'] == args.plan_sha256 and
                all(admission[key] == plan[key] for key in IDENTITY),
                'execution admission identity differs')
        records, stages, failures, missing, totals_by_group = {}, {}, [], [], {}
        previous_end = None
        started_ids = []
        for name, group in groups.items():
            start = R / (name + '-controller-start.json')
            decision_path = R / (name + '-decision.json')
            if not start.exists():
                require(not decision_path.exists(), 'decision without started service')
                missing.append(name)
                continue
            require(not missing, 'started service is not a frozen prefix')
            started_ids.append(name)
            begin = reader.read(start)
            end = reader.read(R / (name + '-controller-exit.json'))
            stage = reader.read(R / (name + '-stage.json'))
            identity(stage, plan, args.plan_sha256, name)
            require(positive(begin['started_t']) and positive(end['ended_t']) and
                    begin['started_t'] == end['started_t'] == stage['started_t'] and
                    begin['started_t'] < end['ended_t'] <= stage['ended_t'] <=
                    report['started_t'] and end['cleanup_complete'] is True and
                    stage['cleanup_complete'] is True and
                    stage['first_runtime_test'] is (name == plan['group_ids'][0]),
                    name + ': service is active, unclean or time identity differs')
            if previous_end is not None:
                require(previous_end <= begin['started_t'], 'service order overlaps')
            previous_end = stage['ended_t']
            stages[name] = stage
            if stage['returncode'] != 0 or stage['failure'] is not None:
                failures.append(dict(path=str(R / (name + '-stage.json')),
                    category='controller', returncode=stage['returncode'],
                    failure=stage['failure']))
            if not decision_path.exists():
                require(stage['returncode'] != 0 or stage['failure'] is not None,
                        name + ': successful service lacks final audit')
                continue
            record = reader.read(decision_path)
            require(record['group_id'] == name and record['plan_sha256'] == args.plan_sha256,
                    name + ': audit scope differs')
            if not record['passed']:
                require(record.get('failure'), 'failed audit lacks first failure')
                failures.append(dict(path=str(decision_path), category='group_audit',
                                     failure=record['failure']))
                continue
            identity(record, plan, args.plan_sha256, name)
            require(record['failure'] is None and record['kind'] == group['kind'] and
                    record['mirror_gpu_recycle'] == group['arm'] and
                    record['arm'] == ('B' if group['arm'] else 'A') and
                    record['request_count'] == len(record['requests']) == group['requests'] and
                    record['ended_t'] == stage['ended_t'] <= record['recorded_t'] <=
                    report['started_t'], name + ': final audit identity/count differs')
            require(stage['returncode'] == 0 and stage['failure'] is None,
                    'passed audit overlays failed controller')
            request_ids = [row['response_id'] for row in record['requests']]
            require(len(set(request_ids)) == len(request_ids), 'duplicate request id')
            if group['kind'] != 'quality':
                require(len(record['metrics']) == group['requests'], 'missing timing rows')
                for index, (metric, row) in enumerate(zip(record['metrics'], record['requests'])):
                    if group['kind'] == 'history':
                        position, round_index = index % 7 + 1, index // 7 + 1
                        length = HISTORY[position - 1]
                        require(metric['position'] == position and metric['round'] ==
                                round_index and metric['repeat'] == 1, 'history order changed')
                        case = f'sequence-r{round_index:02d}-p{position:02d}-context-{length}'
                        output_tokens = 256
                    else:
                        length = group['input_tokens']
                        output_tokens = 257 if length == 261887 else 256
                        case = 'context-' + str(length)
                        require(metric['repeat'] == index + 1, 'matrix repeat order changed')
                    require(metric['case'] == case and metric['input_tokens'] == length and
                            row['actual_input'] == length and row['actual_output'] ==
                            output_tokens and row['finish'] == ['length'], 'HTTP usage differs')
            totals_by_group[name] = counters(record, group)
            records[name] = record
        require(started_ids == plan['group_ids'][:len(started_ids)],
                'non-prefix execution')
        quality_name = plan['group_ids'][0]
        if quality_name in records:
            oracle_path = plan['quality_reference_path']
            reader.external.add(str(Path(oracle_path).resolve()))
            oracle = reader.read(oracle_path, plan['quality_reference_sha256'])
            refs = {row['prompt_sha256']: row for row in oracle}
            require(len(refs) == 11, 'quality oracle count differs')
            for row in records[quality_name]['requests']:
                require(all(row[key] == refs[row['prompt_sha256']][key]
                            for key in OUTPUT_FIELDS), 'quality differs from bound oracle')
        direct = {}
        for kind in ('host', 'numerical'):
            first = R / (kind + '-first-attempt.json')
            if not first.exists():
                require(not (R / (kind + '-decision.json')).exists(),
                        kind + ': decision without first attempt')
                continue
            original = reader.read(first)
            identity(original, plan, args.plan_sha256, kind)
            require(quality_name in records and original['started_t'] >=
                    records[quality_name]['recorded_t'], 'direct work preceded quality pass')
            if kind == 'numerical':
                require('host' in direct and direct['host']['passed'] and
                        original['started_t'] >= direct['host']['recorded_t'],
                        'numerical preceded host pass')
            for receipt in original['records']:
                require(receipt['cleanup_complete'] is True,
                        kind + ': direct process cleanup incomplete')
            if original['passed']:
                require(reader.read(R / (kind + '-decision.json')) == original,
                        kind + ': first passing attempt was replaced')
                require(sum(group['passed'] for group in original['groups']) ==
                        (24 if kind == 'host' else 1), 'direct contract count differs')
            else:
                require(original.get('failure'), 'failed direct batch lacks failure')
                failures.append(dict(path=str(first), category=kind,
                                     failure=original['failure']))
            direct[kind] = original
        if any(groups[name]['kind'] != 'quality' for name in started_ids):
            require(all(kind in direct and direct[kind]['passed']
                        for kind in ('host', 'numerical')), 'performance before direct pass')
            require(stages[started_ids[1]]['started_t'] >= direct['numerical']['recorded_t'],
                    'performance preceded numerical terminal')
        static_findings = []
        for path, digest in plan['frozen_files'].items():
            candidate = Path(path)
            if candidate.parent == R and 'finding' in candidate.name and candidate.suffix == '.json':
                finding = reader.read(candidate, digest)
                static_findings.append(dict(path=str(candidate),
                    status=finding.get('status'), finding=finding.get('finding')))
        # Discover only new root/direct receipt paths; never descend source/docs or old evidence.
        owner_failures = []
        receipt_paths = list(R.glob('*-exit.json'))
        for directory in ('host-01', 'numerical-01', 'pipeline-execution'):
            receipt_paths.extend((R / directory).glob('*-exit.json'))
        require(len(receipt_paths) <= 256, 'unexpectedly unbounded receipt count')
        for path in sorted(set(receipt_paths)):
            receipt = reader.read(path)
            if receipt.get('returncode') not in (None, 0) or receipt.get('failure'):
                owner_failures.append(dict(path=str(path), returncode=receipt.get('returncode'),
                                          failure=receipt.get('failure')))
        report.update(plan_sha256=args.plan_sha256,
            runtime_source_commit=plan['runtime_source_commit'],
            runtime_binary_sha256=plan['runtime_binary_sha256'],
            started_service_count=len(started_ids), successful_audited_service_count=len(records),
            accepted_http_request_count=sum(record['request_count'] for record in records.values()),
            unrun_groups=missing, failed_or_unaudited_groups=[name for name in started_ids
                                                          if name not in records],
            direct_status={kind: value['passed'] for kind, value in direct.items()},
            counters_by_group=totals_by_group, retained_static_findings=static_findings,
            retained_contract_failures=failures, retained_owner_failures=owner_failures)
        if args.mode == 'early':
            require(failures, 'early stop needs a current retained service/audit/direct failure')
            require(len(records) < 17 or not all(value['passed'] for value in direct.values()),
                    'fully complete run requires complete review mode')
            for name in ('history-decision.json', 'performance-decision.json'):
                path = R / name
                if path.exists():
                    value = reader.read(path)
                    require(value.get('performance_acceptance') is not True and
                            value.get('default_enable_allowed') is not True,
                            'early stop cannot assert performance acceptance')
            report.update(status='PASS_EARLY_STOP_ACCOUNTING',
                full_frozen_coverage_completed=False,
                independent_performance_decision='NOT_EVALUABLE_INCOMPLETE',
                missing_is_not_pass=True)
        else:
            require(started_ids == plan['group_ids'] and len(records) == 17 and
                    all(kind in direct and direct[kind]['passed']
                        for kind in ('host', 'numerical')) and not failures,
                    'complete review requires all final passed HTTP/direct contracts')
            history = reader.read(R / 'history-decision.json')
            performance = reader.read(R / 'performance-decision.json')
            for label, record in (('history', history), ('performance', performance)):
                identity(record, plan, args.plan_sha256, label)
                require(record['failure'] is None and record['evidence_contracts_passed'] is True and
                        record['default_enable_allowed'] is False and
                        record['whole_physical_RAM_54GB'] == 'INDETERMINATE',
                        label + ': acceptance boundary differs')
            for saved, names in ((history, [name for pair in PAIRS for name in pair]),
                                 (performance, plan['group_ids'])):
                for name in names:
                    path = str((R / (name + '-decision.json')).resolve())
                    require(saved['source_sha256'].get(path) == reader.sources[path],
                            'comparison ledger references a different group decision')
            for name in ('h02-on', 'h03-on', 'h04-off'):
                rows_equal(records['h01-off']['requests'], records[name]['requests'],
                           'all-history-output')
            independent_history, history_failures = [], []
            for block_index, (off, on) in enumerate(PAIRS):
                saved = history['blocks'][block_index]
                require(saved['baseline_group'] == off and saved['candidate_group'] == on,
                        'history pair direction differs')
                block = []
                for position in range(7):
                    indices = (position, position + 7, position + 14)
                    before = [records[off]['metrics'][index] for index in indices]
                    after = [records[on]['metrics'][index] for index in indices]
                    result = gate(before, after, f'{off}/{on}:p{position + 1}')
                    compare_saved_gate(saved['positions'][position], result)
                    block.append(result)
                    history_failures.extend(result['label'] + ':' + key
                                            for key in result['failed_checks'])
                require(saved['passed'] is all(row['passed'] for row in block),
                        'history block aggregate differs')
                independent_history.append(block)
            history_passed = not history_failures
            require(history['passed'] is history_passed and
                    history['failed_checks'] == history_failures and
                    history['decision'] == ('PASS_SCREENING' if history_passed else 'NO_GO'),
                    'history veto differs from recomputed gates')
            tiers, tier_failures = [], []
            for index, length in enumerate(LENGTHS):
                selected = {group['arm']: group['id'] for group in groups.values()
                            if group['kind'] == 'matrix' and group['input_tokens'] == length}
                require(set(selected) == {0, 1}, 'matrix pair missing')
                off, on = selected[0], selected[1]
                rows_equal(records[off]['requests'], records[on]['requests'], str(length))
                result = gate(records[off]['metrics'], records[on]['metrics'],
                              'context-' + str(length))
                compare_saved_gate(performance['tiers'][index], result)
                tiers.append(result)
                tier_failures.extend(result['label'] + ':' + key for key in result['failed_checks'])
            changed = sum(values['changed'] for values in totals_by_group.values())
            failures_expected = history_failures + tier_failures
            if changed == 0:
                failures_expected.append('actual_selection_change_not_observed')
            perf_passed = not failures_expected
            require(performance['passed'] is perf_passed and
                    performance['failed_checks'] == failures_expected and
                    performance['all_speed_gates_passed'] is (not bool(history_failures + tier_failures))
                    and performance['actual_intervention_gate_passed'] is (changed > 0) and
                    performance['history_veto_retained'] is (not history_passed) and
                    performance['request_count'] == 131 and performance['service_count'] == 17 and
                    performance['full_frozen_coverage_completed'] is True and
                    performance['decision'] == ('PASS_SCREENING' if perf_passed else 'NO_GO'),
                    'aggregate differs from independent extrema/counter gates')
            report.update(status='PASS_COMPLETE_RESULT_REVIEW',
                full_frozen_coverage_completed=True, history=independent_history,
                tiers=tiers, failed_checks=failures_expected, actual_changed=changed,
                independent_performance_decision='PASS_SCREENING' if perf_passed else 'NO_GO',
                no_epsilon_used=True, no_statistical_significance_inferred=True)
        reader.stable()
        report.update(passed=True, interpretation_limits=[
            'Independent arithmetic from final audited JSON metrics; no raw timing remeasurement.',
            'Transitive source ledgers bound through decision SHA, not recursively reread.',
            'First failures present in owned receipts are retained; absence of transient writes is not proven.',
            'Counter selection change is no avoided-read, causal-speed, or full numerical proof.',
            'Resources are not recalculated here. 16GiB memcg is not physical54GB acceptance.',
            'Default-off is the frozen contract and static-reviewed source identity, not a deployment check.'])
        exit_code = 0
    except BaseException as error:
        report.update(status='FAIL_INDEPENDENT_REVIEW',
                      failure=type(error).__name__ + ': ' + str(error))
    finally:
        report.update(ended_t=time.time(), source_sha256=reader.sources,
            metadata_bytes_read=reader.bytes,
            reviewer_script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
        with output.open('x') as destination:
            json.dump(report, destination, indent=2, allow_nan=False)
            destination.write('\n')
        print({key: report.get(key) for key in ('status', 'passed',
            'independent_performance_decision', 'accepted_http_request_count', 'failure')})
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())

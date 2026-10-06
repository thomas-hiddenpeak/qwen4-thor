"""Bounded supply observations, not a cache policy or performance acceptance.

The original phase validator is reused on a copy without the new optional
layer field. Raw records remain intact. Only full snapshots contain observer
state; prefix checkpoints cannot supply per-layer observation deltas.
"""
import hashlib
import json
from pathlib import Path
import re

import audit_offload_diagnostics as phase
from offload_policy import DIAGNOSTIC_ENVIRONMENT, policy_environment

SCOPE = 'offload_decode_supply_observer_v1'
SCHEMA = 'q4t.moe_supply_observer.v1'
GROUP_ORDER = (('AS', False), ('AS', True), ('AL', True), ('AL', False))
COUNTERS = tuple(('plans_started plans_complete plans_failed '
    'plans_scope_mismatch planned_loads source_l2 source_mirror source_read '
    'committed_loads claim_errors read_errors commit_errors entry_l2_present '
    'entry_mirror_present entry_mirror_candidate entry_candidate_to_mirror '
    'entry_candidate_to_l2 entry_candidate_to_read_direct_active '
    'entry_candidate_to_read_direct_published entry_candidate_to_read_other '
    'entry_candidate_unclaimed writeback_reservations '
    'writeback_pending_missing_targets writeback_pending_entry_candidate_targets '
    'writeback_published writeback_aborted writeback_skipped '
    'source_mirror_outside_entry_candidates duplicate_claims '
    'entry_claimed_mirrors counter_overflow samples_confirmed_total '
    'samples_overwritten').split())
OBSERVER_KEYS = {'schema', 'enabled', 'counters', 'samples',
                 'plan_state_bytes', 'persistent_state_bytes'}
SAMPLE_KEYS = set(('layer plan_clock entry_index expert source_task '
    'overwriting_task GPU_victim mirror_slot entry_L2_present '
    'entry_mirror_candidate reserve_seq claim_seq publication_seq_or_zero '
    'state_at_claim publication_final_outcome actual_source read_ok commit_ok '
    'plan_complete witness_seq').split())
ERROR_KEYS = ('plans_failed', 'plans_scope_mismatch', 'claim_errors',
              'read_errors', 'commit_errors', 'duplicate_claims',
              'counter_overflow', 'entry_candidate_unclaimed',
              'entry_candidate_to_read_other')
CANDIDATE_PARTS = ('entry_candidate_to_mirror', 'entry_candidate_to_l2',
    'entry_candidate_to_read_direct_active',
    'entry_candidate_to_read_direct_published', 'entry_candidate_to_read_other',
    'entry_candidate_unclaimed')
MAX_ADDITIONAL_JSON_BYTES = 1 << 20


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest_value(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate supply JSON key: ' + key)
        result[key] = value
    return result


def supply_plan(document, sequence_sha256, max_len=262144):
    """Exactly four requests, A000, old bundle on, only observer alternating."""
    require(type(document) is dict and set(document) ==
            {'schema', 'scope', 'phase_plan_sha256', 'trace_max_mib', 'group'},
            'invalid supply sequence fields')
    require(type(document['schema']) is int and document['schema'] == 1 and
            document['scope'] == SCOPE, 'unsupported supply sequence schema')
    require(digest_value(sequence_sha256) and
            digest_value(document['phase_plan_sha256']),
            'invalid supply sequence or phase SHA256')
    require(type(max_len) is int and max_len == 262144 and
            type(document['trace_max_mib']) is int and
            document['trace_max_mib'] == 128, 'supply capacity or trace cap differs')
    group = document['group']
    keys = {'id', 'index', 'pair_index', 'pair_position', 'condition', 'arm',
            'predecessor', 'predecessor_tokens', 'observation', 'observer',
            'requests'}
    require(type(group) is dict and set(group) == keys,
            'invalid supply group fields')
    index = group['index']
    require(type(index) is int and 1 <= index <= 4, 'invalid supply group index')
    condition, enabled = GROUP_ORDER[index - 1]
    suffix = 'on' if enabled else 'off'
    identity = f's{index:02d}-{condition.lower()}-{suffix}'
    predecessor = 1024 if condition == 'AS' else 8193
    require(group['id'] == identity and group['condition'] == condition and
            group['arm'] == 'A' and group['predecessor'] == condition[1] and
            group['observation'] == 'on' and
            type(group['observer']) is bool and group['observer'] == enabled,
            'supply group identity, bundle or observer differs')
    require(type(group['pair_index']) is int and
            group['pair_index'] == (index + 1) // 2 and
            type(group['pair_position']) is int and
            group['pair_position'] == 1 + (index - 1) % 2 and
            type(group['predecessor_tokens']) is int and
            group['predecessor_tokens'] == predecessor,
            'supply pair position or predecessor differs')
    require(type(group['requests']) is list and len(group['requests']) == 4,
            'supply group requires exactly four requests')
    requests = []
    for position, row in enumerate(group['requests']):
        role = 'conditioning' if position == 0 else 'probe'
        length = predecessor if position == 0 else 1024
        require(type(row) is dict and set(row) ==
                {'position', 'role', 'input_tokens', 'output_tokens'} and
                type(row['position']) is int and row['position'] == position and
                row['role'] == role and type(row['input_tokens']) is int and
                row['input_tokens'] == length and
                type(row['output_tokens']) is int and row['output_tokens'] == 256,
                'supply request order, role or length differs')
        requests.append({'case': f'{identity}-r{position:02d}-{role}-context-{length}',
            'position': position, 'role': role, 'input_tokens': length,
            'max_tokens': 256, 'total_tokens': length + 256, 'repeats': 1,
            'preceding_case': requests[-1]['case'] if requests else None,
            'preceding_input_tokens': requests[-1]['input_tokens']
                if requests else None})
    return {'mode': 'performance', 'scope': SCOPE, 'diagnostic_scope': SCOPE,
        'group_id': identity, 'group_index': index,
        'pair_index': group['pair_index'], 'pair_position': group['pair_position'],
        'condition': condition, 'arm': 'A', 'predecessor': condition[1],
        'predecessor_tokens': predecessor, 'observation': 'on',
        'supply_observer_enabled': enabled, 'phase_diagnostics': True,
        'trace_max_mib': 128, 'sequence_sha256': sequence_sha256,
        'phase_plan_sha256': document['phase_plan_sha256'],
        'lengths': list(dict.fromkeys(row['input_tokens'] for row in requests)),
        'repeats': 1, 'requests': requests, 'partial': True,
        'partial_offload_matrix': True, 'full_five_tier_requested': False,
        'full_offload_matrix_requested': False, 'performance_acceptance': False,
        'client_protocol': 'one_evalscope_process_per_request',
        'client_protocol_note': 'One conditioning and three probes; all old '
            'diagnostics on; no cache resets within service. Observer paired '
            'differences are not pure overhead or performance acceptance.'}


def read_supply_sequence(path, expected_sha256, max_len=262144):
    require(digest_value(expected_sha256), 'invalid expected supply SHA256')
    raw = Path(path).read_bytes()
    require(hashlib.sha256(raw).hexdigest() == expected_sha256,
            'supply sequence SHA256 differs')
    document = json.loads(raw, object_pairs_hook=unique_object)
    return supply_plan(document, expected_sha256, max_len), raw


def observer_environment(enabled, inherited):
    """Shared A000 environment for quality and the four diagnostic services."""
    require(type(enabled) is bool, 'observer setting must be bool')
    result = {key: value for key, value in inherited.items()
              if not key.startswith('Q4T_') and key != 'LD_PRELOAD'}
    result.update(policy_environment(0, 0, 'request-partition-log',
                                    request_partition=0,
                                    decode_partition_log_quiet=0))
    result.update(DIAGNOSTIC_ENVIRONMENT)
    result['Q4T_MOE_SUPPLY_OBSERVER'] = str(int(enabled))
    return result


def supply_environment(plan, inherited):
    require(plan.get('scope') == SCOPE and plan.get('arm') == 'A' and
            plan.get('observation') == 'on' and
            plan.get('phase_diagnostics') is True,
            'invalid supply environment plan')
    return observer_environment(plan['supply_observer_enabled'], inherited)


def check_supply_environment(plan, environment):
    observed = {key: value for key, value in environment.items()
                if key.startswith('Q4T_')}
    require(observed == supply_environment(plan, {}) and
            'LD_PRELOAD' not in environment, 'supply environment differs')


def supply_request_identity(plan, request):
    plan_keys = ('group_id', 'group_index', 'pair_index', 'pair_position',
                 'condition', 'arm', 'predecessor', 'predecessor_tokens',
                 'observation', 'supply_observer_enabled', 'sequence_sha256',
                 'phase_plan_sha256')
    request_keys = ('case', 'position', 'role', 'input_tokens',
                    'preceding_case', 'preceding_input_tokens')
    return {**{key: plan[key] for key in plan_keys},
            **{key: request[key] for key in request_keys}}


def _direct(counters):
    return (counters['entry_candidate_to_read_direct_active'] +
            counters['entry_candidate_to_read_direct_published'])


def _counter_contract(counters, *, true_decode=False):
    require(type(counters) is dict and set(counters) == set(COUNTERS) and
            all(phase.unsigned(value) for value in counters.values()),
            'observer counters missing or not uint64')
    c = counters
    require(all(c[key] == 0 for key in ERROR_KEYS),
            'observer error, scope mismatch, unknown loss or incomplete plan')
    require(c['plans_started'] == c['plans_complete'],
            'observer plans unfinished')
    require(c['planned_loads'] == c['committed_loads'] ==
            c['source_l2'] + c['source_mirror'] + c['source_read'],
            'observer source/commit closure differs')
    require(c['planned_loads'] <= 10 * c['plans_complete'],
            'observer plan miss bound exceeded')
    require(c['entry_mirror_candidate'] == sum(c[key] for key in CANDIDATE_PARTS),
            'entry candidate partition differs')
    require(c['entry_mirror_candidate'] <= c['entry_mirror_present'] <=
            c['planned_loads'] and c['entry_l2_present'] <= c['planned_loads'] and
            c['entry_mirror_candidate'] + c['entry_l2_present'] <=
            c['planned_loads'], 'entry candidate/presence bounds differ')
    require(c['source_mirror'] == c['entry_candidate_to_mirror'] +
            c['source_mirror_outside_entry_candidates'] and
            c['entry_candidate_to_l2'] <= c['source_l2'] and
            _direct(c) <= c['source_read'], 'entry source partition differs')
    require(c['writeback_pending_entry_candidate_targets'] <=
            c['writeback_pending_missing_targets'] <= c['writeback_reservations']
            and _direct(c) <= c['writeback_pending_entry_candidate_targets'],
            'reservation target bounds differ')
    require(c['writeback_published'] + c['writeback_aborted'] ==
            c['writeback_reservations'], 'writeback reservation unfinished')
    require(c['samples_confirmed_total'] == _direct(c),
            'confirmed witness/direct count differs')
    if true_decode:
        require(c['source_mirror_outside_entry_candidates'] == 0 and
                c['entry_claimed_mirrors'] == 0,
                'true decode entry mirror premise violated')


def _sample_contract(sample, layer, clock):
    require(type(sample) is dict and set(sample) == SAMPLE_KEYS,
            'witness fields differ')
    for key, lower, upper in (('layer', layer, layer + 1),
            ('entry_index', 0, 10), ('expert', 0, 512),
            ('source_task', 0, 10), ('overwriting_task', 0, 10),
            ('GPU_victim', 0, 512), ('mirror_slot', 0, 8)):
        require(type(sample[key]) is int and lower <= sample[key] < upper,
                'invalid witness identity: ' + key)
    require(sample['expert'] != sample['GPU_victim'] and
            sample['source_task'] != sample['overwriting_task'],
            'witness victim or task cannot cause direct loss')
    for key in ('plan_clock', 'reserve_seq', 'claim_seq',
                'publication_seq_or_zero', 'witness_seq'):
        require(phase.unsigned(sample[key]), 'invalid witness sequence: ' + key)
    require(0 < sample['plan_clock'] <= clock and sample['witness_seq'] > 0 and
            0 < sample['reserve_seq'] < sample['claim_seq'],
            'witness reserve/claim/plan sequence differs')
    require(sample['entry_L2_present'] is False and
            sample['entry_mirror_candidate'] is True and
            all(sample[key] is True for key in
                ('read_ok', 'commit_ok', 'plan_complete')) and
            sample['actual_source'] == 'READ', 'witness lacks confirmed READ')
    state, final = sample['state_at_claim'], sample['publication_final_outcome']
    publication = sample['publication_seq_or_zero']
    require(state in ('active_reservation', 'published') and
            final in ('published', 'aborted'), 'unknown witness causal state')
    if state == 'published':
        require(final == 'published' and
                sample['reserve_seq'] < publication < sample['claim_seq'],
                'published witness chronology differs')
    elif final == 'published':
        require(sample['claim_seq'] < publication,
                'active reservation publication must follow claim')
    else:
        require(publication == 0, 'aborted reservation has publication sequence')


def _observer_contract(observer, layer):
    require(type(observer) is dict and set(observer) == OBSERVER_KEYS and
            observer['schema'] == SCHEMA and observer['enabled'] is True,
            'observer object fields/schema/enabled differ')
    for key in ('plan_state_bytes', 'persistent_state_bytes'):
        require(type(observer[key]) is int and 0 < observer[key] <= 4096,
                'observer state byte cap exceeded')
    c = observer['counters']
    _counter_contract(c)
    samples = observer['samples']
    total = c['samples_confirmed_total']
    require(type(samples) is list and len(samples) == min(4, total) and
            c['samples_overwritten'] == max(0, total - 4),
            'observer sample retention/overwrite count differs')
    for sample in samples:
        _sample_contract(sample, layer['layer'], layer['slot_clock'])
    require([sample['witness_seq'] for sample in samples] ==
            list(range(max(1, total - 3), total + 1)),
            'retained witness sequence window differs')
    require([sample['plan_clock'] for sample in samples] ==
            sorted(sample['plan_clock'] for sample in samples),
            'retained witness plan order differs')
    for state, key in (('active_reservation',
                       'entry_candidate_to_read_direct_active'),
                      ('published', 'entry_candidate_to_read_direct_published')):
        require(sum(s['state_at_claim'] == state for s in samples) <= c[key],
                'retained witnesses exceed causal counter')


def _observer_phase(before, after, *, true_decode):
    rows = []
    totals = dict.fromkeys(COUNTERS, 0)
    stats = phase.counter_delta(before['stats'], after['stats'],
                                'supply phase stats', phase.BYTE_COUNTERS)
    for a, b in zip(before['layers'], after['layers']):
        old, new = a['supply_observer'], b['supply_observer']
        require(all(old[key] == new[key] for key in
                    ('plan_state_bytes', 'persistent_state_bytes')),
                'observer state sizes changed within service')
        delta = phase.counter_delta(old['counters'], new['counters'],
                                    'observer phase')
        _counter_contract(delta, true_decode=true_decode)
        require(b['slot_clock'] >= a['slot_clock'] and
                b['l2_clock'] >= a['l2_clock'], 'phase cache clock decreased')
        clock_delta = b['l2_clock'] - a['l2_clock']
        observed_l2 = delta['source_l2'] + delta['source_read']
        if true_decode:
            forwards = after['decode_forward_count'] - before['decode_forward_count']
            require(delta['plans_complete'] == forwards and
                    b['slot_clock'] - a['slot_clock'] == forwards,
                    'true decode per-layer plans/clock differ from forwards')
            require(clock_delta == observed_l2,
                    'true decode per-layer L2 clock/source closure differs')
        else:
            require(clock_delta >= observed_l2,
                    'prefill L2 clock below observed singleton supply')
        old_samples = {s['witness_seq']: s for s in old['samples']}
        selected = []
        for sample in new['samples']:
            seq = sample['witness_seq']
            if seq in old_samples:
                require(sample == old_samples[seq], 'retained witness changed')
            if seq > old['counters']['samples_confirmed_total']:
                require(a['slot_clock'] < sample['plan_clock'] <= b['slot_clock'],
                        'new witness outside phase plan window')
                selected.append(sample)
            else:
                require(sample['plan_clock'] <= a['slot_clock'],
                        'old witness has future plan clock')
        for state, key in (('active_reservation',
                           'entry_candidate_to_read_direct_active'),
                          ('published', 'entry_candidate_to_read_direct_published')):
            require(sum(s['state_at_claim'] == state for s in selected) <= delta[key],
                    'phase witnesses exceed causal delta')
        rows.append({'layer': b['layer'], 'counters': delta,
            'l2_clock_delta': clock_delta, 'retained_phase_samples': selected,
            'confirmed_phase_samples_not_retained':
                delta['samples_confirmed_total'] - len(selected)})
        for key in COUNTERS:
            totals[key] += delta[key]
    require(sum(row['l2_clock_delta'] for row in rows) ==
            stats['l2_hits'] + stats['l2_misses'],
            'aggregate raw L2 clock/source closure differs')
    require(stats['loads'] == stats['l2_hits'] + stats['l2_misses'] +
            stats['mirror_hits'], 'original aggregate source closure differs')
    require(totals['planned_loads'] == stats['shape_single_misses'] and
            totals['source_l2'] == stats['l2_shape_single_hits'] and
            totals['source_read'] == stats['l2_shape_single_misses'],
            'observer single-shape/original counters differ')
    if true_decode:
        require(totals['planned_loads'] == stats['loads'] and
                totals['source_l2'] == stats['l2_hits'] and
                totals['source_read'] == stats['l2_misses'] and
                totals['source_mirror'] == stats['mirror_hits'] and
                totals['plans_complete'] == stats['resolve_calls'] and
                totals['writeback_published'] == stats['mirror_writebacks'] and
                totals['writeback_skipped'] == stats['mirror_skips'],
                'true decode observer/original source or writeback closure differs')
    else:
        require(totals['source_mirror'] <= stats['mirror_hits'] and
                totals['writeback_published'] <= stats['mirror_writebacks'] and
                totals['writeback_skipped'] <= stats['mirror_skips'],
                'prefill singleton observations exceed whole-prefill counters')
    return {'scope': 'actual_decode' if true_decode else 'singleton_prefill_only',
        'counters': totals, 'layers': rows,
        'direct_read_losses': _direct(totals),
        'true_decode_entry_premise_applied': true_decode,
        'prefix_layer_deltas_available': False}


def audit_supply_record(record, response, enabled, diagnostic=False):
    """Validate one complete HTTP record, retaining all raw original fields."""
    require(type(enabled) is bool and type(diagnostic) is bool,
            'observer and diagnostic flags must be bool')
    require(type(record) is dict and type(record.get('snapshots')) is list,
            'supply record missing snapshots')
    for key in ('actual_input', 'actual_output'):
        require(type(response.get(key)) is int and response[key] > 0,
                'invalid HTTP token count')
    if diagnostic:
        require(response['actual_output'] == 256 and response.get('finish') == ['length'],
                'fixed diagnostic output differs from 256/length')
    stripped = dict(record)
    stripped['snapshots'] = []
    additional_bytes = 0
    observer_bytes = 0
    require(('supply_observer_serialization' in record) == enabled,
            'observer serialization presence differs from setting')
    for snapshot in record['snapshots']:
        require(type(snapshot) is dict, 'invalid phase snapshot object')
        copy = dict(snapshot)
        if isinstance(snapshot.get('layers'), list):
            copy['layers'] = []
            for layer in snapshot['layers']:
                require(type(layer) is dict, 'invalid phase cache layer')
                require(('supply_observer' in layer) == enabled,
                        'observer presence differs from explicit setting')
                original = dict(layer)
                if enabled:
                    observer = original.pop('supply_observer')
                    _observer_contract(observer, layer)
                    # WriteCache emits compact JSON with this exact added field.
                    size = len(json.dumps(observer, separators=(',', ':'),
                                          ensure_ascii=True).encode())
                    observer_bytes += size
                    additional_bytes += size + len(',"supply_observer":')
                copy['layers'].append(original)
        stripped['snapshots'].append(copy)
    result = phase.audit_record(stripped, response)
    result['phase_record'] = record
    result['supply_observer_enabled'] = enabled
    result['performance_acceptance'] = False
    if enabled:
        begin, boundary, end = (record['snapshots'][0], record['snapshots'][1],
                                record['snapshots'][-1])
        result['supply_observer'] = {
            'schema': SCHEMA,
            'prefill': _observer_phase(begin, boundary, true_decode=False),
            'decode': _observer_phase(boundary, end, true_decode=True)}
        serialization = record['supply_observer_serialization']
        require(type(serialization) is dict and set(serialization) ==
                {'bytes', 'limit_bytes', 'complete'} and
                serialization['complete'] is True and
                type(serialization['bytes']) is int and
                serialization['bytes'] == observer_bytes and
                type(serialization['limit_bytes']) is int and
                serialization['limit_bytes'] == 1 << 20,
                'observer serialization count/completeness differs')
        additional_bytes += len((',"supply_observer_serialization":' +
            json.dumps(serialization, separators=(',', ':'))).encode())
    require(additional_bytes <= MAX_ADDITIONAL_JSON_BYTES,
            'observer additional JSON byte cap exceeded')
    result['observer_additional_json_bytes'] = additional_bytes
    return result


def read_supply_records(path, sources):
    """Strict JSON reader; no network or model work and no silent key loss."""
    path = Path(path)
    digest, records = hashlib.sha256(), {}
    with path.open('rb') as stream:
        for raw in stream:
            digest.update(raw)
            line = raw.decode('utf-8').rstrip('\n')
            if '[q4t][offload_diag]' not in line:
                continue
            require(line.startswith(phase.PREFIX), 'malformed diagnostic log prefix')
            record = json.loads(line[len(phase.PREFIX):], object_pairs_hook=unique_object)
            require(type(record) is dict, 'invalid diagnostic record object')
            identity = record.get('request_id')
            require(type(identity) is str and identity and identity not in records,
                    'missing or duplicate diagnostic request ID')
            records[identity] = record
    sources[str(path.resolve())] = digest.hexdigest()
    return records


def audit_supply_group(records, responses, enabled, kind, expected_inputs=None):
    """One closed service: quality11 or a frozen four-request diagnostic cell."""
    require(kind in ('quality', 'diagnostic'), 'unknown supply group kind')
    count = 11 if kind == 'quality' else 4
    require(type(records) is dict and type(responses) is list and
            len(records) == len(responses) == count,
            'supply group request count differs')
    ids = [row.get('response_id') for row in responses]
    require(all(type(identity) is str for identity in ids) and
            len(set(ids)) == count and set(ids) == set(records),
            'supply group HTTP/diagnostic IDs differ')
    if kind == 'diagnostic':
        require(expected_inputs in ([1024] * 4, [8193, 1024, 1024, 1024]) and
                [row['actual_input'] for row in responses] == expected_inputs,
                'diagnostic conditioning/probe sequence differs')
    else:
        require(enabled is True, 'quality observer must be on')
    results = [audit_supply_record(records[row['response_id']], row, enabled,
                                   diagnostic=kind == 'diagnostic')
               for row in responses]
    for previous, current in zip(responses, responses[1:]):
        before = records[previous['response_id']]['snapshots'][-1]
        after = records[current['response_id']]['snapshots'][0]
        require(before['stats'] == after['stats'],
                'cache statistics reset/changed between requests')
        require(before['layers'] == after['layers'],
                'cache/observer state reset/changed between requests')
    return {'scope': SCOPE, 'kind': kind, 'observer_enabled': enabled,
            'requests': results, 'performance_acceptance': False,
            'causal_limit': 'Observed successful direct READ losses only; '
                'not indirect cache history, exclusive wait, physical SSD '
                'or a paired pure-overhead estimate.'}

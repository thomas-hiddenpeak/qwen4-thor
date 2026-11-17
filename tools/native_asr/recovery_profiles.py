"""Acceptance-only observation of real cold-profile choices and durable budgets.

This module is not shipped in service bundles. It records the sealed policy's
unchanged inputs/result; it never supplies memory or selects a launch profile.
"""
import copy
import json
import os
from pathlib import Path
import time


def observe(plan, payload, output):
    if not plan.get('observe_cold_profiles'):
        return
    from tools.serve_recovery import ledger, profile_contract, profile_memory
    original = profile_memory.cold
    path = Path(output)/('cold-profiles-'+payload+'.jsonl')
    count = 0

    def cold(registry, state, observation):
        nonlocal count
        before = copy.deepcopy(state)
        sampled = copy.deepcopy(observation)
        result = original(registry, state, observation)
        if result.get('reason') == 'cold_memory_capacity':
            row = dict(payload=payload, monotonic_ns=time.monotonic_ns(),
                module=profile_memory.__file__,
                registry_sha256=profile_contract.digest(registry),
                state=before, observation=sampled, result=copy.deepcopy(result))
            raw = (json.dumps(row, separators=(',', ':'))+'\n').encode()
            ledger.require(count < 128 and len(raw) <= 65536 and
                (not path.exists() or path.stat().st_size+len(raw) <= 1024**2),
                'cold profile observation bound')
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                ledger.require(os.write(fd, raw) == len(raw),
                               'cold profile observation incomplete')
            finally:
                os.close(fd)
            count += 1
        return result

    profile_memory.cold = cold


def continuity(before, after, registry, observations, generation):
    from tools.serve_recovery import ledger, profile_contract, profile_memory
    from tools.serve_recovery import profile_policy as policy
    policy.validate(before)
    policy.validate(after)
    bound = profile_contract.digest(registry)
    for value in (before, after):
        ledger.require(value['registry_sha256'] == bound and
            value['profiles'] == {n: ledger.digest(p)
                for n, p in registry['profiles'].items()},
            'recovery profile qualification changed')
    for key in ('format', 'registry_sha256', 'profiles', 'configuration'):
        ledger.require(before[key] == after[key], 'recovery profile contract changed')
    ledger.require(before['selected'] == 'primary' and
        before['pending'] is None and after['pending'] is None and
        after['revision'] >= before['revision'] and
        after['clock']['boot_id'] == before['clock']['boot_id'] and
        after['clock']['logical_ns'] >= before['clock']['logical_ns'] and
        after['clock']['monotonic_ns'] >= before['clock']['monotonic_ns'],
        'recovery profile state or clock changed')
    protected = ('history', 'pending', 'selected', 'not_before_ns',
                 'light_since_ns', 'completed_generation', 'history_bindings')
    if after['selected'] == 'primary':
        ledger.require(all(before.get(k) == after.get(k) for k in protected),
                       'recovery erased original profile budget')
        return dict(kind='unchanged_primary')
    ledger.require(after['selected'] == 'light' and
        after['completed_generation'] == generation and
        after['history'][:-1] == before['history'] and
        len(after['history']) == len(before['history'])+1,
        'recovery lost history or added unplanned switches')
    added = after['history'][-1]
    ledger.require(added['reason'] == 'cold_memory_capacity' and
        added['source'] == 'primary' and added['target'] == 'light' and
        added['source_generation'] < generation,
        'recovery profile change is not qualified cold pressure')
    for row in observations:
        if row.get('payload') != 'fallback' or row.get('registry_sha256') != bound:
            continue
        state = row['state']
        policy.validate(state)
        if any(state.get(k) != before.get(k) for k in
               (*protected, 'registry_sha256', 'profiles', 'configuration', 'format')):
            continue
        result = profile_memory.cold(registry, state, row['observation'])
        if result != row['result'] or result != dict(
                ready=False, target='light', reason='cold_memory_capacity'):
            continue
        clock = state['clock']
        stamp_ns = clock['monotonic_ns']+added['at_logical_ns']-clock['logical_ns']
        ledger.require(ledger.integer(row['monotonic_ns']) and
            0 <= stamp_ns-row['monotonic_ns'] <= 2*ledger.SECOND,
            'cold profile decision is not tied to the observed sample')
        expected = policy.prepare(state, 'light', added['reason'],
            added['request_id'], added['source_generation'],
            ledger.Stamp(clock['boot_id'], stamp_ns))
        ledger.require(expected['history'] == after['history'] and
            expected['not_before_ns'] == after['not_before_ns'] and
            added['at_logical_ns'] <= after['light_since_ns'] <=
                after['clock']['logical_ns'],
            'cold profile switch lost cooldown or history')
        if 'history_bindings' in expected:
            ledger.require(all(after['history_bindings'].get(k) == v
                for k, v in before['history_bindings'].items()),
                'cold profile switch erased historical bindings')
        return dict(kind='observed_cold_memory_downgrade',
            available_bytes=profile_memory.available(row['observation']),
            primary_required_bytes=registry['qualifications']['primary']['startup_required_bytes'],
            light_required_bytes=registry['qualifications']['light']['startup_required_bytes'],
            added_switch=added, history_preserved=True, cooldown_preserved=True)
    raise ValueError('cold profile switch lacks its real decision observation')

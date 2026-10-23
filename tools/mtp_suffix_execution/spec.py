"""Fixed execution-observation matrix; importing it has no external effects."""

LONG = [('context-8192', 1), ('context-204800', 1)]
REPEAT = [('repeat-code', 3), ('repeat-text', 3)]
STEPS = [
    ('first-http', 'full', True, 'first_http_gate', LONG[:1], None),
    ('full-off', 'full', False, 'normal_diagnostic', REPEAT, None),
    ('full-on', 'full', True, 'normal_diagnostic', REPEAT, None),
    ('subset-off-a1', 'subset', False, 'normal_diagnostic', LONG + REPEAT, None),
    ('subset-on-b', 'subset', True, 'normal_diagnostic', LONG + REPEAT, None),
    ('subset-off-a2', 'subset', False, 'normal_diagnostic', LONG + REPEAT, None),
    ('exec-off-a1', 'subset', False, 'execution_diagnostic', LONG, None),
    ('exec-on-b', 'subset', True, 'execution_diagnostic', LONG, None),
    ('exec-off-a2', 'subset', False, 'execution_diagnostic', LONG, None),
    ('nsys8-off', 'subset', False, 'nsys_profile', LONG[:1], 8170),
    ('nsys8-on', 'subset', True, 'nsys_profile', LONG[:1], 8170),
    ('nsys200-on', 'subset', True, 'nsys_profile', LONG, 204781),
    ('nsys200-off', 'subset', False, 'nsys_profile', LONG, 204781),
]


def validate(plan):
    if (plan['http_requests'], plan['http_loads'],
            plan['stage_timeout_seconds']) != (49, 13, 7200):
        raise ValueError('frozen observation-stage budget changed')
    if (plan['retries'] != 0 or plan['warmups'] != 0 or
            plan['runtime_optimization_candidates'] != 0 or
            plan['observation_build_limit'] != 1 or
            plan['direct_contract_timeout_seconds'] != 0 or
            len(plan['steps']) != len(STEPS)):
        raise ValueError('observation scope or service count changed')
    for actual, (name, head, suffix, role, cases, capture) in zip(plan['steps'], STEPS):
        observed = role in ('execution_diagnostic', 'nsys_profile')
        if (actual['name'], actual['head'], actual['suffix'],
                actual['measurement_role'], actual['execution_diagnostic'],
                actual['profile_input_tokens'], actual['kind']) != (
                    name, head, suffix, role, observed, capture,
                    'timing' if observed else 'block'):
            raise ValueError('frozen observation service order/mode changed')
        if ([(c['id'], c['repeats']) for c in actual['cases']] != cases or
                any(c['role'] != 'development_diagnostic' or c['output_tokens'] != 256
                    for c in actual['cases']) or
                actual['http_requests'] != sum(n for _, n in cases) or
                actual['http_loads'] != 1 or
                actual['timeout'] != (1200 if capture else 900)):
            raise ValueError('frozen observation case budget changed')

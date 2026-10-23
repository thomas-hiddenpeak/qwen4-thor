"""Fixed 56-request repair follow-up; no imports with external effects."""

LONG = [('context-8192', 1), ('context-45056', 1), ('context-204800', 1)]
NORMAL = LONG + [('repeat-code', 3), ('repeat-text', 3)]
STEPS = [
    ('first-http', 'full', True, 'first_http_gate', LONG[:1]),
    ('subset-off-a1', 'subset', False, 'normal_diagnostic', NORMAL),
    ('subset-on-b', 'subset', True, 'normal_diagnostic', NORMAL),
    ('subset-off-a2', 'subset', False, 'normal_diagnostic', NORMAL),
    ('full-on', 'full', True, 'normal_diagnostic', NORMAL),
    ('full-off', 'full', False, 'normal_diagnostic', NORMAL),
    ('first45-off', 'subset', False, 'first_long_control', LONG[1:2]),
    ('first45-on', 'subset', True, 'first_long_control', LONG[1:2]),
    ('first200-on', 'subset', True, 'first_long_control', LONG[2:]),
    ('first200-off', 'subset', False, 'first_long_control', LONG[2:]),
    ('timing-off', 'subset', False, 'phase_diagnostic', LONG),
    ('timing-on', 'subset', True, 'phase_diagnostic', LONG),
]


def validate(plan):
    if (plan['http_requests'], plan['http_loads'],
            plan['stage_timeout_seconds']) != (56, 12, 5400):
        raise ValueError('frozen repair-stage budget changed')
    if len(plan['steps']) != len(STEPS):
        raise ValueError('frozen repair-stage service count changed')
    for actual, (name, head, suffix, role, cases) in zip(plan['steps'], STEPS):
        timing = role == 'phase_diagnostic'
        if (actual['name'], actual['head'], actual['suffix'],
                actual['measurement_role'], actual['timing'],
                actual['kind']) != (name, head, suffix, role, timing,
                                    'timing' if timing else 'block'):
            raise ValueError('frozen repair-stage sequence changed')
        if ([(c['id'], c['repeats']) for c in actual['cases']] != cases or
                any(c['role'] != 'development_diagnostic'
                    for c in actual['cases']) or
                actual['http_requests'] != sum(n for _, n in cases) or
                actual['http_loads'] != 1 or actual['timeout'] != 900):
            raise ValueError('frozen repair-stage case budget changed')

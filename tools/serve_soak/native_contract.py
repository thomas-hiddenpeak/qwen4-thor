"""Pure bounded contracts for the native service soak, without deployment I/O."""
import copy
import hashlib
import json

DOCUMENT = 'docs/SERVE_MEMORY_STOP_2026-11-03.md'
PROFILES = {
    'short': dict(seconds=60, cycles=3, interval=20, wall_seconds=600),
    # Respect the delivered alpha RPM/TPM policy. This is sustained business,
    # not a saturation benchmark or permission to raise production quotas.
    'day': dict(seconds=86400, cycles=1440, interval=60, wall_seconds=87900),
}
GROWTH = dict(rss=64*1024**2, anon=128*1024**2, fds=2, threads=2)
WARMUP = 16
LABELS = ('json', 'sse', 'schema', 'tool', 'answer')


def require(value, reason):
    if not value:
        raise ValueError(reason)


def configuration(profile):
    require(type(profile) is str and profile in PROFILES, 'unknown soak profile')
    config = dict(PROFILES[profile])
    return dict(profile=profile, **config, growth=dict(GROWTH), warmup=WARMUP,
        limits=dict(starts=1, chats=config['cycles']*5+1, probes=0,
                    controls=5000 if profile == 'short' else 500000,
                    seconds=config['wall_seconds']))


def validate_plan(plan):
    require(plan.get('version') == 1 and
            plan.get('scenario') == 'native_soak_'+plan.get('profile', ''),
            'invalid native soak plan')
    expected = configuration(plan['profile'])
    require(all(plan.get(k) == v for k, v in expected.items()),
            'frozen soak contract changed')
    require(plan.get('automatic_request_replay') is False and
            plan.get('day_run_owner') == 'user', 'soak execution owner differs')
    return expected


def requests(model):
    common = dict(model=model, temperature=0, enable_thinking=False,
                  max_completion_tokens=64, stream=False)
    plain = dict(common, messages=[dict(role='user',
        content='Compute 17+25. Reply only with the decimal integer.')])
    schema = dict(common, messages=[dict(role='user',
        content='Compute 17+25 as JSON.')], response_format=dict(
        type='json_schema', json_schema=dict(name='arithmetic', strict=True,
            schema=dict(type='object', properties=dict(answer=dict(
                type='integer', const=42)), required=['answer'],
                additionalProperties=False))))
    tool = dict(common, max_completion_tokens=128,
        messages=[dict(role='user', content='Use project_document to read '+
            DOCUMENT+'. Then report only the first 12 hexadecimal digits '
            'of its actual SHA256.')], tools=[dict(type='function', function=dict(
            name='project_document', strict=True,
            description='Read an actual project document and return its SHA256.',
            parameters=dict(type='object', properties=dict(path=dict(
                type='string', const=DOCUMENT)), required=['path'],
                additionalProperties=False)))], tool_choice='required',
        parallel_tool_calls=False)
    return dict(json=plain, sse=dict(copy.deepcopy(plain), stream=True,
        stream_options=dict(include_usage=True)), schema=schema, tool=tool)


def answer_request(tool_request, message, content):
    calls = message.get('tool_calls')
    require(type(calls) is list and len(calls) == 1 and
            calls[0].get('type') == 'function' and
            calls[0]['function']['name'] == 'project_document' and
            json.loads(calls[0]['function']['arguments']) == dict(path=DOCUMENT),
            'actual project tool call differs')
    digest = hashlib.sha256(content).hexdigest()
    result = dict(path=DOCUMENT, sha256=digest, answer=digest[:12])
    reply = dict(role='tool', tool_call_id=calls[0]['id'],
                 content=json.dumps(result))
    request = copy.deepcopy(tool_request)
    request.update(tool_choice='none', stream=True,
                   stream_options=dict(include_usage=True))
    request['messages'] += [message, reply]
    return request, reply, digest


def validate_completion(label, completion, digest=None):
    message = completion['message']
    finish = completion['finish_reason']
    if label == 'tool':
        require(finish == 'tool_calls', 'tool response did not complete')
        return
    require(finish == 'stop' and not message.get('tool_calls'),
            'unexpected completion terminal')
    text = message.get('content', '').strip()
    if label == 'schema':
        require(json.loads(text) == dict(answer=42), 'structured answer differs')
    else:
        require(text == (digest[:12] if label == 'answer' else '42'),
                'actual business answer differs')


def resource_envelope(rows):
    require(len(rows) > WARMUP and all(set(row) == set(GROWTH) and all(
        type(v) is int and v >= 0 for v in row.values()) for row in rows),
        'incomplete model-process resource samples')
    baseline = {k: max(r[k] for r in rows[:WARMUP]) for k in GROWTH}
    maximum = {k: max(r[k] for r in rows[WARMUP:]) for k in GROWTH}
    require(all(maximum[k] <= baseline[k]+GROWTH[k] for k in GROWTH),
            'model-process quiescent resource growth exceeded bounds')
    return dict(samples=len(rows), baseline=baseline, maximum=maximum,
        growth=dict(GROWTH), physical_ram_peak_proved=False,
        scope='one original model process; RSS and RssAnon overlap')


def checkpoint(profile, cycles, elapsed, *, final=False):
    config = configuration(profile)
    target = config['cycles'] if final else 3600//config['interval']
    seconds = config['seconds'] if final else 3600
    require((final or profile == 'day') and type(cycles) is int and
            cycles == target and seconds <= elapsed <= seconds+900,
            'soak checkpoint duration or cycle count incomplete')
    return dict(profile=profile, cycles=cycles, business_requests=cycles*5,
        actual_tool_executions=cycles, seconds=elapsed,
        day_scale_stability_proved=profile == 'day' and final)

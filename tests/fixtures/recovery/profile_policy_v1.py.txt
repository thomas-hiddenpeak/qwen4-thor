"""Durable, bounded profile intentions; never grants model startup authority.

The controller still needs a Guardian grant, whole-tree cleanup and actual
device/lease proof. A prepared intent is retryable state, not a new restart
budget. Time across boots earns no cooldown or switch-budget credit.
"""
import copy

from tools.serve_recovery import ledger, profile_contract

MAX_BYTES = 128*1024
SECOND = ledger.SECOND
HOUR = 60*ledger.MINUTE
DAY = ledger.DAY
FORMAT = 'q4t-profile-selection-v1'
REASONS = ('cold_memory_capacity', 'sustained_memory_pressure',
           'confirmed_memory_fault', 'sustained_memory_recovery')
DEFAULTS = dict(version=1, degrade_below_mib=8192, recover_above_mib=16384,
    pressure_seconds=30, recovery_seconds=120, minimum_light_seconds=300,
    cooldown_seconds=300, switches_per_hour=2, switches_per_day=4)
BOUNDS = dict(degrade_below_mib=(2048,65536),recover_above_mib=(2049,131072),
    pressure_seconds=(2,300),recovery_seconds=(30,1800),
    minimum_light_seconds=(60,86400),cooldown_seconds=(60,3600),
    switches_per_hour=(1,2),switches_per_day=(1,4))


def revision(value, *, minimum=0):
    return ledger.integer(value, minimum=minimum)


def configuration(value):
    ledger.keys(value, DEFAULTS)
    ledger.require(type(value['version']) is int and value['version'] == 1,
                   'invalid adaptive profile policy version')
    for name,(low,high) in BOUNDS.items():
        ledger.require(ledger.integer(value[name]) and low <= value[name] <= high,
                       'invalid adaptive profile policy: '+name)
    ledger.require(value['recover_above_mib'] > value['degrade_below_mib'] and
        value['switches_per_day'] >= value['switches_per_hour'],
        'invalid adaptive profile hysteresis or budget')
    return copy.deepcopy(value)


def initial(registry, stamp, config=None):
    profile_contract.validate(registry)
    config = configuration(DEFAULTS if config is None else config)
    return validate(dict(format=FORMAT,revision=0,
        registry_sha256=profile_contract.digest(registry),
        profiles={name:ledger.digest(profile) for name,profile in registry['profiles'].items()},
        configuration=config,selected='primary',pending=None,
        clock=dict(boot_id=stamp.boot_id,monotonic_ns=stamp.monotonic_ns,logical_ns=0),
        not_before_ns=0,light_since_ns=None,completed_generation=0,history=[]))


def validate(value):
    ledger.keys(value, ('format','revision','registry_sha256','profiles',
        'configuration','selected','pending','clock','not_before_ns',
        'light_since_ns','completed_generation','history'))
    ledger.require(value['format'] == FORMAT and ledger.integer(value['revision']),
                   'invalid profile selection state')
    configuration(value['configuration'])
    ledger.keys(value['profiles'],profile_contract.NAMES)
    for h in [value['registry_sha256'],*value['profiles'].values()]:
        ledger.require(type(h) is str and ledger.HASH.fullmatch(h),
                       'invalid profile selection binding')
    ledger.require(value['selected'] in profile_contract.NAMES and
        ledger.integer(value['not_before_ns']) and
        ledger.integer(value['completed_generation']), 'invalid selected profile')
    clock=value['clock'];ledger.keys(clock,('boot_id','monotonic_ns','logical_ns'))
    ledger.Stamp(clock['boot_id'],clock['monotonic_ns'])
    now=clock['logical_ns'];ledger.require(ledger.integer(now),'invalid profile clock')
    light=value['light_since_ns']
    ledger.require((value['selected']=='primary' and light is None) or
        (value['selected']=='light' and ledger.integer(light) and light <= now),
        'invalid light residency timestamp')
    history=value['history']
    ledger.require(type(history) is list and len(history) <= 4,
                   'unbounded profile switch history')
    prior=-1;ids=set()
    for row in history:
        ledger.keys(row,('request_id','source','target','reason','source_generation',
                         'at_logical_ns','target_sha256'))
        ledger.require(ledger.identifier(row['request_id']) and row['request_id'] not in ids and
            row['source'] in profile_contract.NAMES and row['target'] in profile_contract.NAMES and
            row['source'] != row['target'] and row['reason'] in REASONS and
            ((row['target']=='primary') == (row['reason']=='sustained_memory_recovery')) and
            ledger.integer(row['source_generation']) and
            ledger.integer(row['at_logical_ns']) and prior <= row['at_logical_ns'] <= now and
            row['target_sha256'] == value['profiles'][row['target']],
            'invalid profile switch history entry')
        prior=row['at_logical_ns'];ids.add(row['request_id'])
    pending=value['pending']
    if pending is not None:
        ledger.require(history and pending == history[-1] and
            pending['source'] == value['selected'] and
            pending['source_generation'] >= value['completed_generation'],
            'invalid pending profile intention')
    ledger.require(len(ledger.canonical(value)) <= 32*1024,'profile state byte bound')
    return value


def advanced(previous, stamp):
    result=copy.deepcopy(validate(previous));clock=result['clock']
    elapsed=0
    if clock['boot_id']==stamp.boot_id:
        ledger.require(stamp.monotonic_ns >= clock['monotonic_ns'],
                       'profile monotonic clock moved backwards')
        elapsed=stamp.monotonic_ns-clock['monotonic_ns']
    now=clock['logical_ns']+elapsed
    ledger.require(ledger.integer(now),'profile logical clock overflow')
    clock.update(boot_id=stamp.boot_id,monotonic_ns=stamp.monotonic_ns,logical_ns=now)
    return result


def resume(previous, stamp):
    """Rebase a new controller/worker session without crediting downtime.

    A durable receipt is required before selecting or preparing a launch.
    Pending intent, residency and hourly/daily history remain untouched.
    """
    state = copy.deepcopy(validate(previous))
    ledger.Stamp(stamp.boot_id, stamp.monotonic_ns)
    clock = state['clock']
    if clock['boot_id'] == stamp.boot_id:
        ledger.require(stamp.monotonic_ns >= clock['monotonic_ns'],
                       'profile monotonic clock moved backwards')
    clock.update(boot_id=stamp.boot_id, monotonic_ns=stamp.monotonic_ns)
    state['revision'] += 1
    return validate(state)


def permission(previous, target, stamp):
    state=advanced(previous,stamp);now=state['clock']['logical_ns']
    config=state['configuration']
    reason=None
    if state['pending'] is not None:reason='profile_intent_pending'
    elif target not in profile_contract.NAMES:reason='unknown_profile'
    elif target == state['selected']:reason='profile_already_selected'
    elif now < state['not_before_ns']:reason='profile_cooldown'
    elif target == 'primary' and now-state['light_since_ns'] < config['minimum_light_seconds']*SECOND:
        reason='minimum_light_residency'
    elif sum(now-r['at_logical_ns'] < HOUR for r in state['history']) >= config['switches_per_hour']:
        reason='profile_hour_budget'
    elif sum(now-r['at_logical_ns'] < DAY for r in state['history']) >= config['switches_per_day']:
        reason='profile_day_budget'
    return dict(allowed=reason is None,reason=reason or 'eligible',logical_ns=now)


def prepare(previous,target,reason,request_id,generation,stamp):
    check=permission(previous,target,stamp)
    ledger.require(check['allowed'],check['reason'])
    state=advanced(previous,stamp);now=state['clock']['logical_ns']
    ledger.require(reason in REASONS and
        ((target=='primary') == (reason=='sustained_memory_recovery')) and
        ledger.identifier(request_id) and ledger.integer(generation) and
        generation >= state['completed_generation'] and
        all(row['request_id'] != request_id for row in state['history']),
        'invalid new profile intention')
    pending=dict(request_id=request_id,source=state['selected'],target=target,
        reason=reason,source_generation=generation,at_logical_ns=now,
        target_sha256=state['profiles'][target])
    state.update(revision=state['revision']+1,pending=pending,
        not_before_ns=now+state['configuration']['cooldown_seconds']*SECOND,
        history=[*[row for row in state['history'] if now-row['at_logical_ns'] < DAY],pending])
    return validate(state)


def finish(previous,generation,profile_sha256,stamp):
    state=advanced(previous,stamp);pending=state['pending']
    ledger.require(pending is not None and ledger.integer(generation) and
        generation > pending['source_generation'] and
        profile_sha256 == pending['target_sha256'],
        'new generation has not confirmed the pending profile')
    state.update(revision=state['revision']+1,selected=pending['target'],pending=None,
        completed_generation=generation,light_since_ns=(state['clock']['logical_ns']
            if pending['target']=='light' else None))
    return validate(state)


def transition(previous,candidate):
    validate(previous);validate(candidate)
    stamp=ledger.Stamp(candidate['clock']['boot_id'],candidate['clock']['monotonic_ns'])
    if (candidate['pending'] == previous['pending'] and
            candidate['selected'] == previous['selected']):
        expected = resume(previous, stamp)
    elif previous['pending'] is None:
        row=candidate['pending'];ledger.require(row is not None,'missing profile intention')
        expected=prepare(previous,row['target'],row['reason'],row['request_id'],
                         row['source_generation'],stamp)
    else:
        expected=finish(previous,candidate['completed_generation'],
                        candidate['profiles'][candidate['selected']],stamp)
    ledger.require(candidate==expected,'profile transition changed protected state')
    return candidate


def selected_for_launch(value):
    validate(value)
    return value['pending']['target'] if value['pending'] is not None else value['selected']


def digest(value):return ledger.digest(validate(value))


def describe(value):
    validate(value)
    return dict(revision=value['revision'],registry_sha256=value['registry_sha256'],
        selected=value['selected'],launch_profile=selected_for_launch(value),
        pending=copy.deepcopy(value['pending']),not_before_ns=value['not_before_ns'],
        logical_ns=value['clock']['logical_ns'],switches_retained=len(value['history']),
        model_actions=False)

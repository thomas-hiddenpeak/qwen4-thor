"""Fixed public host-observation projection; missing values are never zero."""
import math
import re

STATES=('WAITING','STARTING','RUNNING','STOPPING','STOPPED','FAILED','UNKNOWN')
REASONS=('none','binding_invalid','source_unavailable','source_stale','owner_absent',
         'boot_changed','resource_observation_invalid','host_resource_oom',
         'host_resource_tasks','host_resource_observation_unavailable','backend_failure')
KINDS=('none','current','final')
STALE_SECONDS=30


class Invalid(RuntimeError):pass


def require(value,code):
    if not value:raise Invalid(code)


def integer(value):
    require(type(value) is int and 0<=value<=2**53-1,'resource_observation_invalid')
    return value


def number(value):
    require(type(value) in (int,float) and math.isfinite(value) and value>=0,'source_stale')
    return value


def snapshot(raw,policy):
    required=('memory.current','memory.peak','memory.swap.current','pids.current')
    result={name:integer(raw[name]) for name in required}
    for name,keys in (('memory.events',('low','high','max','oom','oom_kill')),
                       ('pids.events',('max',))):
        require(type(raw[name]) is dict and set(keys)<=raw[name].keys(),'resource_observation_invalid')
        result[name]={key:integer(raw[name][key]) for key in keys}
    wanted={'memory.high':policy['memory_high_bytes'],'memory.max':policy['memory_max_bytes'],
            'memory.swap.max':0,'memory.oom.group':1,'pids.max':policy['tasks_max']}
    require(raw['effective_limits']==wanted and all(type(v) is int for v in raw['effective_limits'].values()),
            'resource_observation_invalid')
    result['effective_limits']=wanted
    # memory.current can transiently exceed memory.max; preserve the kernel
    # reading. It is not an invented sum with CUDA requested allocations.
    return result


def view(state,reason='none',*,kind='none',values=None,ready=None,cleanup=None,source_age=None):
    require(state in STATES and reason in REASONS and kind in KINDS,'binding_invalid')
    require((values is None)==(kind=='none'),'resource_observation_invalid')
    return dict(state=state,reason=reason,kind=kind,resources=values,backend_ready=ready,
                cleanup_confirmed=cleanup,source_age_seconds=source_age)


def metrics(value,deployment,generation):
    require(re.fullmatch(r'[A-Za-z0-9_-]{1,64}',deployment) and
            re.fullmatch(r'[0-9a-f]{64}',generation),'binding_invalid')
    labels=f'deployment="{deployment}",generation="{generation}"'
    lines=[]
    def emit(name,help_,rows,kind='gauge'):
        full='q4t_host_'+name
        lines.extend((f'# HELP {full} {help_}',f'# TYPE {full} {kind}'))
        for extra,amount in rows:
            require(type(amount) in (int,float) and math.isfinite(amount) and amount>=0,'resource_observation_invalid')
            lines.append(f'{full}{{{labels}{","+extra if extra else ""}}} {amount}')
    emit('evaluated_timestamp_seconds','Wall time of this observer evaluation, not backend freshness.',[('',value['evaluated_unix'])])
    emit('state','Lifecycle observation; RUNNING does not imply API readiness.',[(f'state="{v}"',int(value['state']==v)) for v in STATES])
    emit('reason','Fixed observation or terminal failure reason.',[(f'reason="{v}"',int(value['reason']==v)) for v in REASONS])
    emit('snapshot_kind','Current kernel or historical final counters; none means unavailable.',[(f'kind="{v}"',int(value['kind']==v)) for v in KINDS])
    emit('resource_observation_valid','Whether numeric resource evidence is available in the stated snapshot kind.',[('',int(value['resources'] is not None))])
    if value['source_age_seconds'] is not None:
        emit('source_age_seconds','Age of the live worker observation in the same boot.',[('',value['source_age_seconds'])])
    if value['backend_ready'] is not None:
        emit('backend_ready','Readiness from a fresh bound worker record.',[('',int(value['backend_ready']))])
    if value['cleanup_confirmed'] is not None:
        emit('cleanup_confirmed','Historical controller cleanup confirmation; not a GPU probe.',[('',int(value['cleanup_confirmed']))])
    data=value['resources']
    if data is not None:
        for suffix,key,help_ in (
            ('memory_current_bytes','memory.current','Host memory charged to this cgroup; excludes uncharged CUDA allocations.'),
            ('memory_peak_bytes','memory.peak','Kernel host accounting peak for this generation; not total physical RAM.'),
            ('swap_current_bytes','memory.swap.current','Swap charged to this cgroup.'),
            ('tasks_current','pids.current','Kernel process and thread count in this cgroup.')):
            emit(suffix,help_,[('',data[key])])
        for suffix,key in (('memory_high_bytes','memory.high'),('memory_max_bytes','memory.max'),('tasks_max','pids.max')):
            emit(suffix,'Configured bound of this host accounting domain.',[('',data['effective_limits'][key])])
        emit('memory_events_total','Kernel events within the bound generation; final counters remain historical.',
             [(f'event="{k}"',v) for k,v in data['memory.events'].items()],'counter')
        emit('tasks_limit_events_total','Kernel pids limit refusals within the bound generation.',[('',data['pids.events']['max'])],'counter')
    return '\n'.join(lines)+'\n'

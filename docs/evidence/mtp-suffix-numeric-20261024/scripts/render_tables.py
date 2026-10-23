"""Render an existing, post-stage numeric-suffix analysis; no model/HTTP calls.

All values come from analysis.json. This is presentation only: it cannot accept
the candidate, invent missing measurements, or replace per-block protection.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path

BASE = Path(__file__).resolve().parent
CASES = [f'context-{n}' for n in [1024,4096,8192,45056,204800]] + [
    'old-8k','repeat-code','repeat-text','old-repeat-code','old-repeat-text',
    'nonrepeat','numeric-enum']
LABELS = {'context-1024':'新1K','context-4096':'新4K','context-8192':'新8K',
    'context-45056':'新45K','context-204800':'新200K','old-8k':'旧8K（开发）',
    'repeat-code':'新重复代码','repeat-text':'新重复文本',
    'old-repeat-code':'旧重复代码（开发）','old-repeat-text':'旧重复文本（开发）',
    'nonrepeat':'新非重复','numeric-enum':'新数字枚举'}


def finite(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)


def number(value,digits=4,scale=1):
    return f'{value/scale:.{digits}f}' if finite(value) else '—'


def integer(value):
    return str(value) if isinstance(value,int) and not isinstance(value,bool) else '—'


def boolean(value):
    return '是' if value is True else '否' if value is False else '—'


def percent(value):
    if not finite(value):
        return '—'
    arrow='↑' if value>0 else '↓' if value<0 else '='
    return f'{value:+.3f}% {arrow}'


def histogram(value):
    return ','.join(map(str,value)) if isinstance(value,list) and len(value)==4 else '—'


def mean(stats):
    count,total=stats.get('count'),stats.get('sum')
    return total/count if finite(count) and count>0 and finite(total) else None


def escape(value):
    return str(value).replace('|','\\|').replace('\n','<br>')


def table(lines,headers,rows):
    lines.append('| '+' | '.join(map(escape,headers))+' |')
    lines.append('| '+' | '.join('---' for _ in headers)+' |')
    lines.extend('| '+' | '.join(map(escape,row))+' |' for row in rows)
    if not rows:
        lines.append('| '+' | '.join(['未记录']+['—']*(len(headers)-1))+' |')
    lines.append('')


def heading(lines,text):
    lines.extend(['## '+text,''])


def object_text(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True) if value is not None else '—'


def render(data,source,source_sha,tool_sha):
    lines=['# 数字格式准入：冻结阶段数据表','',
        f'输入：`{source}`，SHA256 `{source_sha}`。',
        f'渲染脚本 SHA256：`{tool_sha}`。',
        f'受测源码：`{data.get("source_commit","—")}`；候选：`{data.get("candidate_sha256","—")}`；'
        f'父程序：`{data.get("parent_sha256","—")}`。','',
        '本表只呈现冻结样本。decode 为 (输出 token−1)/(latency−TTFT)，越高越好；'
        'TTFT 单位秒，越低越好。箭头表示 ON 相对 OFF 的数值方向，并非统计显著性。'
        '每档值为同一次服务内三请求中位数；场景为一次请求。'
        'GM 为两个启动配对比的几何均值，不能抵消任何块、档或 head 的可信保护回退。'
        '旧材料为开发回归；独立 timing 不计入验收吞吐。缺项或不适用记“—”。','']
    heading(lines,'预算、完整性与四项决定字段')
    budget=data.get('budget',{})
    table(lines,['字段','记录'],[
        ['阶段状态',data.get('state',{}).get('status','—')],
        ['外层 lease',object_text(data.get('lease'))],
        ['请求',f'{integer(data.get("observed_http"))}/{integer(data.get("expected_http"))}'],
        ['启动',f'{integer(data.get("state",{}).get("loads_started"))}/{integer(data.get("expected_services"))}'],
        ['阶段墙钟/上限（秒）',f'{number(data.get("state",{}).get("elapsed_seconds"),3)} / {integer(budget.get("frozen",{}).get("stage_timeout_seconds"))}'],
        ['合同墙钟/上限（秒）',f'{number(data.get("state",{}).get("contract_seconds"),3)} / {integer(budget.get("frozen",{}).get("direct_contract_timeout_seconds"))}'],
        ['预算各项均在界内',boolean(budget.get('all_within_frozen_bounds'))],
        ['报告检查完整',boolean(data.get('measurement_complete'))],
        ['缺项/不一致条数',str(len(data.get('missing_or_partial',[])))],
    ]+[[key,object_text(value)] for key,value in data.get('decision_fields',{}).items()])
    lines.extend(['接受后的 3 次健康请求/3 次启动是另行条件预算，本表不假定已经执行。',
        '两块仍不足以证明总体非劣；首请求和所有请求原值见后表，原始完整 hash 见 analysis.json。',''])

    pairs=data.get('paired_comparisons',{})
    for head in ['full','subset']:
        cases=pairs.get(head,{})
        for metric,title,unit in [('decode_tps','decode','tok/s'),('ttft','TTFT','秒')]:
            heading(lines,f'{head}：{title}（{unit}）')
            rows=[]
            for name in CASES:
                entry=cases.get(name,{})
                blocks={b['block']:b for b in entry.get('blocks',[])}
                row=[LABELS[name]]
                for b in [1,2]:
                    m=blocks.get(b,{}).get(metric,{})
                    row.extend([number(m.get('off_median')),number(m.get('on_median')),percent(m.get('change_pct'))])
                aggregate=entry.get('aggregate',{}).get(metric,{})
                row.extend([number(aggregate.get('geometric_mean_ratio'),6),percent(aggregate.get('change_pct'))])
                rows.append(row)
            table(lines,['场景','B1 OFF','B1 ON','B1 Δ','B2 OFF','B2 ON','B2 Δ','配对 GM 比','GM Δ'],rows)
        heading(lines,f'{head}：输入/输出身份与冻结目标')
        rows=[]
        head_goals=data.get('goals',{}).get(head,{})
        for name in CASES:
            entry=cases.get(name,{})
            goal=head_goals.get('repeat_scenarios',{}).get(name,{})
            if name=='old-8k':
                goal=head_goals.get('old_8k',{})
            rows.append([LABELS[name],entry.get('role','—'),boolean(entry.get('complete')),
                boolean(entry.get('all_blocks_input_identity_equal')),
                boolean(entry.get('all_blocks_output_identity_equal')),
                boolean(goal.get('frozen_goal_met',goal.get('frozen_runtime_gate_met')))])
        table(lines,['场景','材料角色','两块齐全','输入一致','输出一致','本项冻结目标满足'],rows)
        lines.extend(['“目标满足”只指该重复或旧 8K 单项，不代表候选接受；其余项须独立审查保护。',''])

    heading(lines,'普通 decode：同期父程序与候选')
    rows=[]
    for name,entry in data.get('plain_comparisons',{}).items():
        d=entry.get('decode_tps',{});t=entry.get('ttft',{})
        rows.append([LABELS.get(name,name),number(d.get('off_median')),number(d.get('on_median')),
            percent(d.get('change_pct')),number(t.get('off_median')),number(t.get('on_median')),
            percent(t.get('change_pct')),boolean(entry.get('input_hashes_equal')),
            boolean(entry.get('output_hashes_equal'))])
    table(lines,['场景','父 tok/s','候选 tok/s','decode Δ','父 TTFT 秒','候选 TTFT 秒','TTFT Δ','输入同','输出同'],rows)
    lines.extend(['普通模式只有同期父→候选一对启动；三请求不构成三次独立启动。',''])

    heading(lines,'旧 8K 与新 45K：每请求完整生成工作量')
    rows=[]
    for head in ['full','subset']:
        for name in ['old-8k','context-45056']:
            for block in pairs.get(head,{}).get(name,{}).get('blocks',[]):
                for side,label in [('before_raw','OFF'),('after_raw','ON')]:
                    for r in block.get(side,[]):
                        w=r.get('whole_generation_work') or {};s=r.get('suffix') or {}
                        rows.append([head,LABELS[name],str(block['block']),label,integer(r.get('request_index')),
                            histogram(r.get('accepted_a0_a1_a2_a3')),integer(w.get('verify_calls')),
                            integer(w.get('ordinary_tail_output_tokens')),integer(w.get('ordinary_tail_target_T1')),
                            integer(w.get('total_target_forward_calls')),integer(w.get('target_processed_rows')),
                            integer(w.get('target_restore_calls')),integer(w.get('neural_proposal_T1')),
                            integer(w.get('draft_extend_calls')),integer(s.get('hits')),integer(s.get('numeric_rejected'))])
    table(lines,['head','场景','块','臂','请求','a0,a1,a2,a3','verify','尾输出','尾 T1','目标 forward',
        '目标行','restore','神经 draft T1','draft extend','suffix hit','数字拒绝'],rows)
    lines.extend(['计数含首个 MTP 轮次、排除 prefill/init；不是 latency−TTFT 的计时窗口。'
        '目标 forward=verify+普通尾部 T1，目标行=4×verify+普通尾部 T1。'
        '仅对完整 256 token、length 结束的非终止路径推导。'
        'restore 单列用于诊断，不额外添加冻结门。',''])
    rows=[]
    for head in ['full','subset']:
        for entry in pairs.get(head,{}).get('old-8k',{}).get('old_8k_work_protection',[]):
            delta=entry.get('protected_count_deltas') or {}
            rows.append([head,str(entry['block']),integer(delta.get('verify_calls')),
                integer(delta.get('ordinary_tail_output_tokens')),integer(delta.get('ordinary_tail_target_T1')),
                boolean(entry.get('does_not_increase'))])
    table(lines,['head','块','Δ verify','Δ尾输出','Δ尾 T1','工作不增加门'],rows)

    heading(lines,'各请求原值（常规吞吐；排除质量、取消、独立 timing）')
    rows=[]
    for name,group in data.get('groups',{}).items():
        if group.get('kind') not in ['block','performance']:
            continue
        for case in group.get('cases',[]):
            for r in case.get('raw',[]):
                rows.append([name,LABELS.get(case['id'],case['id']),integer(r.get('server_request_ordinal')),
                    boolean(r.get('first_request_of_service')),integer(r.get('actual_input')),
                    integer(r.get('actual_output')),number(r.get('decode_tps'),6),number(r.get('ttft'),6),
                    number(r.get('latency'),6),number(r.get('post_ttft_seconds'),6),
                    (r.get('output_sha256') or '—')[:12]])
    table(lines,['服务','场景','服务请求序号','服务首请求','输入 tok','输出 tok','decode tok/s','TTFT 秒',
        'latency 秒','post-TTFT 秒','输出 hash 前12位'],rows)

    heading(lines,'共享分类表全生命周期（每个 ON 服务一次）')
    rows=[]
    for name,group in data.get('groups',{}).items():
        if not group.get('suffix_enabled'):
            continue
        events=group.get('shared_class_table',[])
        init=[r for r in events if r.get('event')=='init'];release=[r for r in events if r.get('event')=='release']
        i=init[0] if len(init)==1 else {};r=release[0] if len(release)==1 else {}
        rows.append([name,group.get('kind','—'),integer(i.get('bytes')),integer(i.get('cap_bytes')),
            integer(i.get('unknown')),number(i.get('init_ns'),3,1e6),integer(r.get('bytes')),
            number(r.get('release_ns'),3,1e6),boolean(group.get('table_order_valid'))])
    table(lines,['服务','用途','表 bytes','cap bytes','unknown','初始化 ms','释放 bytes','释放 ms','生命周期顺序'],rows)
    lines.extend(['共享表与请求索引分别报告；OFF 的表不存在，不把缺失计时写成测得 0。',''])

    heading(lines,'请求索引与查询成本（按服务汇总）')
    rows=[]
    for name,group in data.get('groups',{}).items():
        if not group.get('suffix_enabled'):
            continue
        s=group.get('mtp',{}).get('totals',{})
        rows.append([name,group.get('kind','—'),integer(s.get('queries')),integer(s.get('hits')),
            integer(s.get('numeric_rejected')),integer(s.get('peak_bytes')),integer(s.get('object_bytes')),
            number(s.get('init_ns'),3,1e6),number(s.get('append_ns'),3,1e6),number(s.get('query_ns'),3,1e6),
            number(s.get('numeric_ns'),3,1e6),number(s.get('release_ns'),3,1e6),
            number(s.get('query_mean_ns'),3,1e3)])
    table(lines,['服务','用途','查询','命中','数字拒绝','峰值 bytes','对象 bytes',
        '索引 init ms','append ms','query ms','其中 numeric ms','release ms','每查询 μs'],rows)
    lines.extend(['时间列按该服务内实际请求累加；内存列是单请求最大值，不能按请求累加。'
        'numeric_ns 已包含在 query_ns 内，绝不再次相加。匹配/索引 CPU 时间不是 GPU 时间。',''])

    heading(lines,'ON 主矩阵：每场景匹配、准入与接受分布')
    rows=[]
    for name,group in data.get('groups',{}).items():
        if group.get('kind')!='block' or not group.get('suffix_enabled'):
            continue
        for case in group.get('cases',[]):
            s=case.get('suffix_totals',{})
            rows.append([name,LABELS.get(case['id'],case['id']),integer(s.get('queries')),integer(s.get('hits')),
                integer(s.get('numeric_checks')),integer(s.get('numeric_rejected')),integer(s.get('classified_tokens')),
                number(s.get('matched_tokens_per_matched_query'),3),
                histogram((s.get('suffix_acceptance') or {}).get('a0_a1_a2_a3')),
                histogram((s.get('neural_acceptance') or {}).get('a0_a1_a2_a3'))])
    table(lines,['服务','场景','查询','命中','数字检查','数字拒绝','扫描 token','平均匹配长度*',
        'suffix a0,a1,a2,a3','neural a0,a1,a2,a3'],rows)
    lines.extend(['* 平均匹配长度分母为 hits+incomplete+d0_mismatch+numeric_rejected，'
        '是所有已形成至少三 token 匹配的查询，并非成功命中平均长度。',''])

    heading(lines,'独立 timing：按 suffix/neural 分组，不能作为验收吞吐')
    rows=[]
    for name,group in data.get('timing',{}).items():
        for kind,summary in group.get('by_suffix',{}).items():
            for phase,gpu_phase in [('draft','draft'),('verify','verify'),('extend_accept','extend')]:
                wall=summary.get('wall_ms',{}).get(phase,{})
                gpu=summary.get('gpu_stream_elapsed_ms',{}).get(gpu_phase,{})
                rows.append([name,kind,integer(summary.get('steps')),phase,
                    number(wall.get('sum'),3),number(mean(wall),3),
                    number(gpu.get('sum'),3),number(mean(gpu),3),
                    histogram((summary.get('acceptance') or {}).get('a0_a1_a2_a3'))])
    table(lines,['服务','提案路径','轮数','阶段','wall 合计 ms','wall 均值 ms',
        'GPU event 合计 ms','GPU event 均值 ms','a0,a1,a2,a3'],rows)
    lines.extend(['wall extend_accept 是联合区间；GPU extend 是独立事件区间，不能当作相同量直接相减。'
        'GPU event 为流经过时间，可含流空闲，不能称纯 kernel 活跃时间。'
        'draft 区间保留实际插桩定义，不以命名推断包含所有 CPU 查询开销。',''])

    heading(lines,'统一 RAM 与监控')
    resources=data.get('resources',{});nvml=resources.get('nvml',{})
    table(lines,['字段','记录'],[
        ['RAM 样本',integer(resources.get('samples'))],['NVML 查询',integer(nvml.get('queries'))],
        ['NVML 状态',object_text(nvml.get('status_counts'))],['NVML 缺时间戳',integer(nvml.get('missing_timestamp'))],
        ['RAM 最大采样间隙秒',number(resources.get('sample_gap_seconds',{}).get('maximum'),3)]])
    rows=[]
    for key,stats in resources.get('global_system_bytes',{}).items():
        rows.append([key,number(stats.get('minimum'),3,2**30),number(stats.get('median'),3,2**30),number(stats.get('maximum'),3,2**30)])
    table(lines,['系统项','最小 GiB','中位 GiB','最大 GiB'],rows)
    rows=[]
    for name,group in resources.get('groups',{}).items():
        process=group.get('process_status_bytes',{});system=group.get('system_bytes',{})
        rows.append([name,integer(group.get('host_samples')),integer(group.get('nvml_query_samples')),
            number(process.get('VmRSS',{}).get('maximum'),3,2**30),
            number(process.get('VmHWM',{}).get('maximum'),3,2**30),
            number(group.get('nvml_process_used_bytes',{}).get('maximum'),3,2**30),
            number(system.get('MemAvailable',{}).get('minimum'),3,2**30),
            number(system.get('Cached',{}).get('minimum'),3,2**30),number(system.get('Cached',{}).get('maximum'),3,2**30)])
    table(lines,['服务','RAM样本','NVML样本','RSS峰 GiB','HWM峰 GiB','NVML进程峰 GiB',
        'MemAvailable最低 GiB','Cached最低 GiB','Cached最高 GiB'],rows)
    lines.extend([resources.get('interpretation','RAM、NVML、RSS 和文件缓存重叠，不相加。'),
        '各项极值可出现在不同时间；统一内存与共享缓存仍计入物理 RAM，'
        '这里只呈现实际采样，不能把独立峰值之差归因为算法净节省。',''])

    heading(lines,'质量、取消恢复与直接合同')
    table(lines,['服务','质量观察','exact match','预期'],[
        [name,integer((q or {}).get('observed')),integer((q or {}).get('exact_matches')),integer((q or {}).get('expected'))]
        for name,q in data.get('quality',{}).items()])
    table(lines,['服务','取消/恢复记录'],[[name,object_text(value)] for name,value in data.get('lifecycle',{}).items()])
    table(lines,['合同','记录为完成','日志 SHA256','日志路径'],[
        [name,boolean(name in data.get('state',{}).get('completed',[])),group.get('log_sha256') or '—',group.get('log') or '—']
        for name,group in data.get('groups',{}).items() if group.get('kind')=='contract'])
    lines.extend(['合同通过范围以原始日志及复用依赖审计为准；不从输出文本一致推导任意输入状态等价。',''])

    heading(lines,'全部保护项的不利观测方向（不自动等同确认退化）')
    table(lines,['head','场景','块','观测方向需审查'],[
        [row.get('head','—'),LABELS.get(row.get('case'),row.get('case','—')),row.get('block') or '父→候选',
         ', '.join(row.get('metrics',[]))]
        for row in data.get('protections',{}).get('observed_adverse_directions',[])])
    lines.extend(['本列表完整保留小幅方向差；新 45K 的两块、两 head 也逐项保留，'
        '不被总体收益抵消。可信性须结合配对复现、工作量、输出身份与有限样本边界人工评估。',''])
    if data.get('missing_or_partial'):
        heading(lines,'缺项或不一致（不得由部分结果宣称通过）')
        lines.extend(['```json',json.dumps(data['missing_or_partial'],ensure_ascii=False,indent=2),'```',''])
    return '\n'.join(lines)+'\n'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--analysis',type=Path,default=BASE/'analysis.json')
    parser.add_argument('--output',type=Path,default=BASE/'report-tables.md')
    args=parser.parse_args()
    if not args.analysis.is_file():
        raise SystemExit('NOT_RUN: final analysis.json is not available')
    source=args.analysis.read_bytes();data=json.loads(source)
    if data.get('state',{}).get('status') not in ['MEASURED_REQUIRES_REVIEW','STOPPED','ENV_ABORT'] or not data.get('lease'):
        raise SystemExit('NOT_RUN: analysis does not record terminated stage and lease')
    if args.output.exists():
        raise SystemExit('refusing to overwrite existing report; use a separate explicit output')
    source_sha=hashlib.sha256(source).hexdigest()
    tool_sha=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    text=render(data,args.analysis.resolve(),source_sha,tool_sha)
    with args.output.open('x') as f:
        f.write(text)
    print(json.dumps(dict(output=str(args.output),sha256=hashlib.sha256(text.encode()).hexdigest(),
        analysis_sha256=source_sha),indent=2))


if __name__=='__main__':
    main()

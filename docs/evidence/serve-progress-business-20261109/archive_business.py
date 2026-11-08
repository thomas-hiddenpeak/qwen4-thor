import json
from pathlib import Path
import shutil
import stat
from tools.serve_service.package import read, sha

root = Path.cwd()
base = root / '.q4t-work/serve-progress-20261109'
stage = base / 'business-02'
out = root / 'docs/evidence/serve-progress-business-20261109'
out.mkdir(exist_ok=False)
shutil.copyfile(__file__, out / 'archive_business.py')
for folder in ('continue-01', 'business-02/lease'):
    for source in (base / folder).rglob('*'):
        if not stat.S_ISREG(source.lstat().st_mode):
            continue
        dest = out / source.relative_to(base)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
execution = stage / 'execution'
for source in execution.iterdir():
    if not source.is_file() or source.name.startswith('ledger-'):
        continue
    dest = out / source.relative_to(base)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, dest)
for source in execution.rglob('model.log'):
    shutil.copyfile(source, out / 'business-02' / (source.parent.name+'.log'))
shutil.copyfile(stage / 'plan.json', out / 'business-02/plan.json')
result = read(execution / 'result.json')
plan = read(stage / 'plan.json')
assert result['status'] == 'PASS_BOUNDED_RECOVERY_BUSINESS'
assert result['failure'] is None and result['cleanup_failure'] is None
assert read(stage / 'lease/exit.json')['status'] == 'COMPLETED'
assert result['budget']['counts']['starts'] == 3
assert result['budget']['counts']['chats'] == 9
assert result['budget']['counts']['probes'] == 1
before = read(execution / 'owned-main-paused.json')['captured']['snapshot']
confirmed = read(execution / 'progress-fault-confirmed.json')
after = confirmed['snapshot']
fault = confirmed['fault']
first_row = next(r for r in before['rows'] if r['ticket'] == fault['ticket'])
last_row = next(r for r in after['rows'] if r['ticket'] == fault['ticket'])
assert before['identity'] == after['identity']
assert before['sequence'] < after['sequence']
assert first_row == last_row
assert fault['work'] == 8 and fault['confirmed_ns']-fault['since_ns'] >= 2_000_000_000
assert after['observed_ns']-max(last_row['work_since_ns'],
    last_row['last_completed_ns']) >= 60_000_000_000
first, second = [read(execution / ('ready-'+str(n)+'.json')) for n in (1, 2)]
assert first['model_pid'] != second['model_pid']
assert first['guardian_pid'] == second['guardian_pid']
assert first['controller_pid'] == second['controller_pid']
final = read(execution / 'business-final.json')
assert final['model_pid'] == second['model_pid'] and final['business_ready']
assert final['controller_health']['observation'] == 'UNKNOWN'
assert read(execution / 'tool-execution.json')['executions'] == 1
queries = [json.loads(line) for n in ('business-01', 'business-02')
    for line in (base / n / 'lease/nvml-queries.jsonl').read_text().splitlines()]
assert all(x['status'] == 'OK' for x in queries)
assert all(sha(root / n) == h for n, h in plan['sources'].items())
assert all(sha(x['path']) == x['sha256'] for x in plan['protected'])
# Actual privileged policies remain private; never archive their credentials.
secrets = []
for source in [base / 'business-01/gateway-policy.json',
        base / 'business-01/client-policy.json',
        *list((execution / 'state').rglob('client-policy.json'))]:
    value = read(source)
    secrets += [c['api_key'] for c in value.get('clients', []) if 'api_key' in c]
    if 'gateway_token' in value:
        secrets.append(value['gateway_token'])
for path in out.rglob('*'):
    if path.is_file():
        raw = path.read_bytes()
        assert not any(secret.encode() in raw for secret in secrets), str(path)
decision = dict(status='PASS_TARGETED_NATIVE_PROGRESS_RECOVERY_BUSINESS',
    engine_sha256=result['package']['engine_binary_sha256'],
    service_manifest_sha256=result['package']['manifest_sha256'],
    protocol_sha256=read(execution / 'capabilities.json')['protocol_source_sha256'],
    cumulative_model_starts=3, cumulative_chats=9, recovery_probes=1,
    cumulative_seconds=result['seconds']+plan['prior_seconds'],
    nvml_samples=len(queries), all_nvml_ok=True,
    real_work_unchanged_while_fresh_snapshots_advanced=True,
    old_tree_retired_before_replacement=True, recovered_real_document_tool=True,
    observer_lost_same_model_json_sse=True, cleanup_confirmed=True,
    default_changed=False, performance_acceptance=False,
    previous_failed_attempt='serve-progress-discovery-20261109',
    full_R03_complete=False,
    pending=['cache GPU copy is currently hidden by auxiliary cache wait',
        'request allocation/reset/cleanup progress coverage audit',
        'rollback-capable accepted delivery'])
(out / 'decision.json').write_text(json.dumps(decision, indent=2)+'\n')
(out / 'README.md').write_text('''# R03实际主循环恢复与辅助观察失联业务

2026-11-09，候选1f3d0de4。本轮2启动/8生成/1恢复CUDA探针，175.288秒；
加前轮驱动失败累计3启动/9生成/220.941秒，209次外层NVML全OK。
旧FAILED见serve-progress-discovery-20261109，本轮严格租约COMPLETED。

原生JSON/SSE均为42；ptrace仅暂停自有模型主线程，独立发布序号
继续增长、实际循环工作ID和完成行保持不变。超过30秒执行预算及
30秒清理宽限，再经2秒新鲜确认触发execution_deadline。旧树退场、
CUDA探针和配额窗口后第二代ready；控制器和Guardian身份保持。

恢复后实际读取docs/MODEL.md前3行，工具只执行一次且回填引用正确。
关闭该代私有观察接收端后UNKNOWN/READY_DEGRADED，business_ready
仍true，同PID JSON/SSE继续42；总生成计数核对，无重放或额外启动。
持久STOPPED、全部active空、端口及自有进程清理、租约结束均通过。

源/协议/默认身份核对，正式发行默认尚未替换。覆盖复核发现可选
缓存同步GPU复制被缓存等待掩盖，正在补齐并核对请求资源生命周期。
因此本组实际通过不作为R03整体完成；全部Serve计划继续。无完整
性能/全模型质量/长稳测试，真实模型故障用主线程停滞，不声称注入了
物理GPU故障。私有凭据/绑定ledger不归档，原始响应和进度事实保留。
''')
index = {str(p.relative_to(out)): dict(bytes=p.stat().st_size, sha256=sha(p))
    for p in sorted(out.rglob('*')) if p.is_file()}
(out / 'index.json').write_text(json.dumps(index, indent=2)+'\n')
print(json.dumps(dict(files=len(index), status=decision['status'],
    seconds=decision['cumulative_seconds'], nvml_samples=len(queries))))

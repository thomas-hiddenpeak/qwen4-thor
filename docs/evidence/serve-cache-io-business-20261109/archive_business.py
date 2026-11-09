"""Archive raw cache short validation without copying cache directories or credentials."""
import json
from pathlib import Path
import shutil
import stat
import struct
from tools.serve_service.package import read, sha
root = Path.cwd().resolve()
base = root / '.q4t-work/serve-cache-io-20261109'
stage = base / 'business-01'
out = root / 'docs/evidence/serve-cache-io-business-20261109'
result, plan, verdict, cleanup, progress = [read(stage / n) for n in (
    'result.json', 'plan.json', 'business-verdict.json', 'cleanup.json', 'progress-summary.json')]
assert result['status'] == 'PASS_CACHE_IO_AND_RESOURCE_PROGRESS_BUSINESS'
assert result['counts'] == dict(starts=1, chats=7, controls=56) and result['seconds'] < 600
assert result['binary_sha256'] == plan['binary_sha256'] == sha(plan['binary'])
assert verdict['same_model'] and verdict['cache_fault']['prefix'] == 0
assert verdict['cache_fault']['error'] == 'cache storage time limit exceeded'
assert verdict['recovered']['prefix'] == 9032 and verdict['recovered']['error'] == 'none'
assert verdict['mtp_restore']['prefix'] == 8438 and verdict['mtp_restore']['error'] == 'none'
assert verdict['cache_before']['prefix']['generation'] == verdict['cache_after']['prefix']['generation']
assert verdict['cache_after']['session']['workers_reaped'] == 1
assert cleanup['remaining'] == [] and cleanup['model_exit'] == 0
assert cleanup['ports_released'] and cleanup['lease_continuously_held']
assert progress['error'] is None and progress['last']['successes'] == 7
assert progress['last']['active'] == 0 and progress['cache_wait_samples'] == 30
raw=(stage/'progress.bin').read_bytes(); off=frames=payload=0
while off < len(raw):
    size=struct.unpack_from('<I',raw,off)[0];off+=4
    assert size >= 184 and off+size <= len(raw)
    off+=size;frames+=1;payload+=size
assert frames == progress['frames'] == 393 and payload == progress['bytes'] == 131928
queries=[json.loads(line) for line in (base/'lease-business-01/nvml-queries.jsonl').read_text().splitlines()]
assert len(queries) == 93 and all(x['status']=='OK' for x in queries)
assert queries[-1]['pids'] == [] and read(base/'lease-business-01/exit.json')['status']=='COMPLETED'
assert all(sha(root/n)==h for n,h in plan['sources'].items())
assert all(sha(n)==h for n,h in plan['protected'].items())
reservations=[json.loads(line.split('] ',1)[1]) for line in (stage/'model.log').read_text().splitlines() if line.startswith('[startup-reservation] ')]
last=reservations[-1]
assert last['event']=='finalized' and last['status']=='PASS'
assert last['allocated']==last['consumed']==last['freed']==1619
assert last['unhanded_remaining']==last['handed_live']==last['release_unknown']==0
out.mkdir(exist_ok=False)
shutil.copyfile(__file__,out/'archive_business.py')
for folder in ('business-01','lease-business-01'):
    for source in (base/folder).iterdir():
        if not stat.S_ISREG(source.lstat().st_mode):continue
        destination=out/folder/source.name;destination.parent.mkdir(exist_ok=True)
        shutil.copyfile(source,destination)
decision=dict(status='ACCEPT_TARGETED_CACHE_IO_AND_NATIVE_PROGRESS',
    binary_sha256=result['binary_sha256'],protocol_sha256=read(stage/'capabilities.json')['protocol_source_sha256'],
    source_commit=plan['source_commit'],frozen_sources=len(plan['sources']),
    counts=result['counts'],seconds=result['seconds'],lease_status='COMPLETED',nvml_samples=len(queries),all_nvml_ok=True,
    same_model=True,cache_recovered=True,shared_cache_unchanged=True,mtp_cached_sse=True,
    raw_progress_frames=frames,cache_wait_samples=30,cache_device_samples=0,
    progress_shutdown_unknown=progress['unknown'],startup_reservations_freed=1619,
    allocation_note='Intermediate server_destroyed quota still has 6 entries/125239296 bytes; process exits 0 and final NVML has no compute process. Do not claim that intermediate snapshot proves zero live allocations.',
    cleanup_confirmed=True,default_changed=False,performance_acceptance=False,full_model_quality_run=False,
    reused=['serve-cache-io-cpu-20261109','serve-cache-io-driver-20261109','serve-progress-resource-20261109','serve-progress-business-20261109'],
    progress_sampling='250ms can miss short device/resource scopes; direct CPU bindings reused.',
    remaining=['rollback-capable accepted delivery','all remaining master tasks'])
(out/'decision.json').write_text(json.dumps(decision,indent=2)+'\n')
(out/'README.md').write_text('''# 缓存文件隔离与实际资源进度组合业务

2026-11-09。候选1ff729a0；冻结1启动/8Chat/600秒，实际1启动/7Chat/
98.872秒，同一严格GPU租约COMPLETED，93次NVML全OK，末次无计算进程。
698项来源及默认身份逐项核对，默认尚未替换。

真实JSON cold与shared SSE消息/logprobs相同；共享恢复1024token，私有
目标布局恢复8846token。仅SIGSTOP已核对parent/birth/exe/目录inode
的自有缓存worker，约2002.957ms文件超时后prefix归零，同模型PID重新
生成成功；新代缓存随后恢复9032token，共享缓存代次和可用性保持。
MTP缓存保存后SSE恢复8438token，JSON/SSE输出均为42。七次请求全部
成功，无模型重启、探针、额外生成或未知文件操作重放。

运行判定前392帧，清理后原始393帧/131928载荷字节；最后active=0、
successes=7。实际缓存等待30次采样，没有误判设备停滞；250ms未采到
短GPU复制/资源范围，明确复用针对性CPU，不将采样零值伪称全覆盖。
清理后的8次progress_disconnected记录为UNKNOWN，不冒充运行故障。
此前实际主循环卡死恢复、观察失联同PID工具业务按未变依赖复用。

模型退出0、端口释放、自有进程清空，1619项启动预留全部释放。
server_destroyed中间快照仍有6项/125239296bytes，不能把该时点当全部
GPU分配归零；最终进程退出及NVML空进程是上下文释放证据。

原始请求/响应、SDK重组、实际配置/指标/进度和故障身份全部保留。
只归档普通文件，不拷缓存目录或凭据。无完整性能/全模型质量/长稳
验收主张；接着可回退交付，再继续R04/N04及全部Serve总计划。
''')
index={str(p.relative_to(out)):dict(bytes=p.stat().st_size,sha256=sha(p)) for p in sorted(out.rglob('*')) if p.is_file()}
(out/'index.json').write_text(json.dumps(index,indent=2)+'\n')
print(json.dumps(dict(files=len(index),status=decision['status'],nvml_samples=len(queries))))

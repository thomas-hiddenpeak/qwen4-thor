"""Preserve SRV-03b incomplete evidence without upgrading it to acceptance."""
from pathlib import Path
import json,sys
root=Path.cwd()
sys.path.insert(0,str(root))
from tools.serve_backpressure import stage
h,base=stage.h,stage.BASE
for p in (stage.PACKAGE,stage.OUTPUT): h.verify_seal(p)
assert h.read(stage.OUTPUT/'closed-incomplete.json')['status']=='NOT_ACCEPTED_STRICT_BACKPRESSURE_UNOBSERVED'
dest=root/'docs/evidence/serve-backpressure-20261103'
dest.mkdir(parents=True,exist_ok=False)
paths=[p for name in ('build-record','payload','payload-array','cpu','postmortem-cpu','package','execution') for p in (base/name).rglob('*') if p.is_file()]
paths += [p for p in base.iterdir() if p.is_file() and p.name in ('archive.py','postflight.json','memory-summary.json','memory-interpretation.json')]
records,local={},{}
for path in sorted(paths):
 relative=str(path.relative_to(base));identity=h.identity(path)
 with path.open('rb') as stream: elf=stream.read(4)==b'\x7fELF'
 if elf or path.name=='source.bundle':
  local[relative]=identity
  continue
 target=dest/relative;target.parent.mkdir(parents=True,exist_ok=True)
 h.copy_checked(identity,target);records[relative]=identity
h.save(dest/'source-map.json',records)
h.save(dest/'local-identities.json',local)
(dest/'README.md').write_text(f'''# SRV-03b未通过验收的完整记录

冻结驱动0ba39744、专用ELF903c4f77。默认5642eb68/6aca6a3e保持。
{len(records)}份原始记录按字节复制，另有README、source-map、local-identities和根seal；
{len(local)}份ELF/source.bundle保留原本地路径，不安装专用程序。

本轮1生成/0专门拒绝/1启动，严格输出未在1024 token预算内闭合，
55314.963ms日志记录structured_output_incomplete，未观察到EAGAIN。
驱动等待原170秒观察窗后退出，206.739秒执行，guard COMMAND_FAILED、
无ENV_ABORT；正常清理，原预算不扩大，剩余请求/启动不使用。

第一驱动没有读取并保存错误wire，不能补造其内容或声称HTTP客户端
已验证错误；保留请求、日志、170次内存采样、worker reap与guard记录。
本机没有task/*/children，原采样漏worker，不把缺失当0；日志报告的
worker post-exec VmHWM和含继承峰值的wait4高水位独立记录。

CPU首次8项7通过，构建门选错target的1项失败保留，受影响修复通过。
后续修复驱动在同请求结束后保存失败wire、不再等满观察窗，CPU3项
通过；改proc-stat父子发现并核对start ticks，CPU夹具和实际闲置子进程
2项通过，未加载模型重验。后续修复不能追认原验收通过或补回缺失记录。

06:25:12 CST GPU PID空、共享锁可用、无常驻模型/恢复标记，
MemAvailable116.976GiB。正式边界见[结果](../../SERVE_BACKPRESSURE_RESULT_2026-11-03.md)。
''')
h.seal(dest)
for relative,identity in records.items(): assert h.sha(dest/relative)==identity['sha256']
files=[p for p in dest.rglob('*') if p.is_file()]
print(json.dumps(dict(copied=len(records),local_artifacts=len(local),files=len(files),bytes=sum(p.stat().st_size for p in files))))

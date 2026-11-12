"""Archive only reports and nonsecret metadata, never run configs or keys."""
import json,shutil
from pathlib import Path
ROOT=Path.cwd().resolve()
import sys
sys.path.insert(0,str(ROOT))
from tools.serve_service.package import read,sha
BASE=ROOT/'.q4t-work/serve-native-observation-20261109';OUT=ROOT/'docs/evidence/serve-native-observation-delivery-20261109';OUT.mkdir()

def copy(src,name):
 dst=OUT/name;dst.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(src,dst)
for folder in ('bundle-cpu-01','release-01','packaged-01','packaged-02','packaged-03','delivery-01'):
 for p in sorted((BASE/folder).iterdir()):
  if p.is_file() and p.name!='plan.private.json':copy(p,folder+'/'+p.name)
for n in ('receipt.json','result.json','control-review.json','native-build.json','decision.json'):
 copy(BASE/'release-02'/n,'release-02/'+n)
for n in ('receipt.json','service.release.json','service.json'):
 copy(BASE/'delivery-01/rollback'/n,'delivery-01/rollback/'+n)
for n in ('prepare_delivery.py','packaged_check.py','install_delivery.py','archive_delivery.py'):copy(BASE/n,n)
result=read(BASE/'delivery-01/result.json');assert result['status']=='PASS_NATIVE_OBSERVATION_RETENTION_DEFAULT_DELIVERY'
(OUT/'decision.json').write_text(json.dumps(dict(status='ACCEPT_NATIVE_OBSERVATION_RETENTION_DELIVERY',delivery=result,packaged=read(BASE/'packaged-03/result.json'),targeted_bundle_tests=3,failed_attempts_preserved=['release-01: expected acceptance.py in bundle although it is an out-of-package harness','packaged-01: wrong driver metric prefix; native output was correct','packaged-02: unavailable /proc/task/children interface; driver now checks stat PPid'],production_source_changes_after_packaging=False,engine_and_protocol_business_reused=True,engine_unchanged=True,archive_excludes=['private run configs','API keys','package client policies','sockets','engine binary'],remaining_master_tasks_open=True),indent=2)+'\n')
(OUT/'README.md').write_text('''# 原生观测与受保护留存可回退默认交付

2026-11-09，R04/N06实际v6包CLI/export/retention闭环及默认交付通过。
包b7c25107，服务发布29125ad9；引擎1ff729a0/元数据f44634f5完全未改。
365份引擎来源与原生ELF/来源未改，之前真实模型/进度/缓存业务接受
复用；本轮0模型/0Chat/0设备探针，不扩完整性能或模型质量测试。

真实随包init/observe/metrics、GET/HEAD exporter与独立worker来源
通过，停服事实来自持久STOPPED账本，未知资源不填零。导出子进程
确认回收、端口关闭、观察锁释放。旧v2 CPU合成run经真实CLI迁移、
完整外部归档、计划、删除、幂等resume；0生产run删除，ledger不变。
另3个v6清单/旧v1—v5合同CPU通过。此前原生观察、留存/epoch/历史
依赖的针对证据按来源复用，不重复全套。

保留全部失败：首封装脚本误将包外acceptance harness列为随包来源；
首次CLI脚本期待错误指标前缀，实际正确为q4t_service_business_ready；
第二次CPU驱动使用内核未提供的task/children，改由/proc/stat PPid
核对实际自有worker。仅脚本修复，生产源码与已封包身份保持。

安装在三把拥有锁及持久STOPPED边界完成，仅替换服务入口/两份服务
记录；密钥、配额、service_id、引擎与ledger2216b9dc保持。安装后
help/observe/metrics通过，未自动启动。完整旧v5包保留，回退回执
f5c49225记录原两份文件及不变引擎摘要；按回执恢复服务文件与软链接。

归档只包含报告、摘要、非秘密元数据；私有配置/密钥/套接字不提交。
run限额不等于整个磁盘上限，归档总治理继续R08。R04/N06交付不代表
总目标完成：接着N04密钥/配额与其余全部Serve计划，main尚待Q阶段。
''')
(OUT/'index.json').write_text(json.dumps({str(p.relative_to(OUT)):dict(bytes=p.stat().st_size,sha256=sha(p)) for p in sorted(OUT.rglob('*')) if p.is_file()},indent=2)+'\n')
print(json.dumps(dict(indexed=len(read(OUT/'index.json')),default_manifest=result['manifest_sha256'])))

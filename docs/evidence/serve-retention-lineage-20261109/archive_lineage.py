import hashlib,json,shutil,subprocess
from pathlib import Path
root=Path.cwd().resolve();base=root/'.q4t-work/serve-native-observation-20261109'
out=root/'docs/evidence/serve-retention-lineage-20261109';out.mkdir()
sha=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest()
for name in ['location-cpu-01','lineage-cpu-01']+['default-dependencies-%02d'%n for n in range(1,9)]:
 shutil.copytree(base/name,out/name)
shutil.copyfile(__file__,out/'archive_lineage.py')
(out/'identity.json').write_text(json.dumps(dict(base_commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),sources={n:sha(root/n) for n in ['tools/serve_recovery/'+n for n in ('retention.py','test_retention.py','test_retention_lineage.py')]},default={n:sha(root/'build'/n) for n in ('q4t','q4t.release.json','service.release.json','service-native-state/ledger.json')}),indent=2)+'\n')
(out/'decision.json').write_text(json.dumps(dict(status='PASS_RETENTION_LINEAGE_TARGETED_CPU_PENDING_PACKAGED_DELIVERY',new_tests=4,affected_location_tests=3,actual_default_metadata=133,protected_paths=476,preserved_failed_readonly_attempts=7,production_runs_removed=0,gpu_starts=0,chat_requests=0,default_changed=False,remaining_master_tasks_open=True),indent=2)+'\n')
(out/'README.md').write_text('''# 随包位置与历史发行依赖留存

2026-11-09。4个新增针对CPU与3个路径受影响CPU通过。随包源码目录
不是部署仓库；从service descriptor固定的manifest取仓库，摘要或
边界变化拒绝。历史回退三类清单、两种engine_unchanged、嵌套摘要
与摘要冲突、无父摘要旧元数据变化使整个淘汰计划失效均有直接验证。

真实当前默认只读遍历通过：133份元数据、476个保护路径。之前7次
失败完整保留：逐次发现旧清单/权限/嵌套摘要兼容问题，最后一次是
records变量遮蔽；修复后完成。旧group-writable元数据仅提供保留
引用，不能授予删除资格或执行接受；已有父摘要必须匹配，每份读取
摘要冻结进计划并在删除前重核。未修改旧文件权限或历史内容。

0生产run删除、0模型/Chat、默认保持。location首组在历史格式扩展
之前运行，新增4项覆盖后续依赖变化；未声称相同源码下重跑全部套件。
接着实际v6封包CLI/export/retention组合与可回退交付，所有总任务继续。
''')
(out/'index.json').write_text(json.dumps({str(p.relative_to(out)):dict(bytes=p.stat().st_size,sha256=sha(p)) for p in sorted(out.rglob('*')) if p.is_file()},indent=2)+'\n')
print(len(json.loads((out/'index.json').read_text())))

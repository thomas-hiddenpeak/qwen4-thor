import json
from pathlib import Path
import shutil
import subprocess
from tools.serve_service.package import read,sha
root=Path.cwd().resolve();base=root/'.q4t-work/serve-native-observation-20261109';out=root/'docs/evidence/serve-run-retention-20261109';out.mkdir(exist_ok=False)
for index,code in [(1,1),(2,1),(3,0),(4,0)]:
 name='retention-cpu-0'+str(index)
 assert read(base/name/'result.json')['exit_code']==code
 shutil.copytree(base/name,out/name)
shutil.copyfile(__file__,out/'archive_retention.py')
sources=['tools/serve_recovery/'+n for n in ('retention.py','test_retention.py','service.py','controller.py','bundle.py','test_native_bundle.py')]
(out/'identity.json').write_text(json.dumps(dict(base_commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),current_sources={n:sha(root/n) for n in sources},default={n:sha(root/'build'/n) for n in ('q4t','q4t.release.json','service.release.json','service-native-state/ledger.json')},scope='Each failure and targeted repair retained. First two runs precede current boot-bound provenance; unchanged archive/policy branches are reused, not claimed to run with final bytes.'),indent=2)+'\n')
(out/'decision.json').write_text(json.dumps(dict(status='PASS_CURRENT_EPOCH_RETENTION_CPU_PENDING_MIGRATION_AND_DELIVERY',distinct_cpp_cases=0,distinct_python_cases=10,first_failed=7,second_passed=5,second_failed=2,third_targeted_passed=5,fourth_targeted_passed=2,gpu_starts=0,chat_requests=0,production_runs_removed=0,default_changed=False,pending=['legacy and cross-boot owner epoch migration','actual packaged CLI/export/retention integration','rollback-capable v6 delivery'],remaining_master_tasks_open=True),indent=2)+'\n')
(out/'README.md').write_text('''# 当前启动期run归档与受保护淘汰CPU检查点

2026-11-09。共10个不同Python用例（9留存及1既有应用目录合同）。
首轮7项均在读取受保护sd-pam进程cwd处拒绝，未进行删除；原FAILED保留。
只把同UID当所有权过宽。后续按启动身份收紧，但2项仍因改名残留/未知
目录无法取得原启动上下文而拒绝，其他5项通过；原FAILED继续保留。

复核补boot绑定：controller v3配置在已有一次必要配置写入中携带
可选retention_context；只用于留存，缺失不影响模型运行，旧v1/v2
仍被控制器接受。未验证启动期不借旧tick值声称当前所有权。普通
归档和中断后resume都使用完整私有归档核对原信息。最终补验5项及
2项通过，包括实际归档/删除/中断续做、链接/目录替换拒绝、当前
自有子进程引用保护，以及新上下文缺失/跨boot保护和应用目录不变。

年龄/数量/实际分配字节、未归档现场与回退依赖、旧计划/归档篡改/
新增证据拒绝采用未变分支通过证据，不假称首轮使用最终源码。
所有文件创建/删除仅专属私有CPU夹具；0生产run删除、0模型/0Chat。

R04仍未交付：旧记录及跨boot归档需补可核验迁移，不能以拒绝/保护
代替最终可用清理；随包CLI/export/retention和v6可回退发行继续。
原生默认1ff729a0/v5、STOPPED和原ledger保持；全部Serve总计划继续。
''')
(out/'index.json').write_text(json.dumps({str(p.relative_to(out)):dict(bytes=p.stat().st_size,sha256=sha(p)) for p in sorted(out.rglob('*')) if p.is_file()},indent=2)+'\n')
print('retention checkpoint archived; migration/delivery remain required')

import json
from pathlib import Path
import shutil
import subprocess
from tools.serve_service.package import read,sha
root=Path.cwd().resolve();base=root/'.q4t-work/serve-native-observation-20261109';out=root/'docs/evidence/serve-retention-epoch-20261109';out.mkdir(exist_ok=False)
for name,code in [('epoch-cpu-01',1),('epoch-cpu-02',0)]:
 assert read(base/name/'result.json')['exit_code']==code
 shutil.copytree(base/name,out/name)
shutil.copyfile(__file__,out/'archive_epoch.py')
sources=['tools/serve_recovery/'+n for n in ('retention.py','retention_epoch.py','test_retention_epoch.py','service.py','controller.py','bundle.py','test_native_bundle.py')]
(out/'identity.json').write_text(json.dumps(dict(base_commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),sources={n:sha(root/n) for n in sources},default={n:sha(root/'build'/n) for n in ('q4t','q4t.release.json','service.release.json','service-native-state/ledger.json')}),indent=2)+'\n')
(out/'decision.json').write_text(json.dumps(dict(status='PASS_RETENTION_EPOCH_TARGETED_CPU_PENDING_PACKAGED_DELIVERY',new_cases=5,reused_affected_cases=2,first_passed=6,first_failed=1,targeted_rerun_passed=1,first_failure='Fixture incremented revision without mandatory transaction receipt; production correctly rejected malformed ledger. Fixture now uses real ledger.transition.',core_file_io_during_locks=False,actual_cpu_flock=True,gpu_starts=0,chat_requests=0,production_runs_removed=0,default_changed=False,pending=['actual packaged CLI/export/retention integration','rollback-capable v6 delivery'],remaining_master_tasks_open=True),indent=2)+'\n')
(out/'README.md').write_text('''# 旧run与跨boot留存身份迁移CPU

2026-11-09，5个新增迁移用例、2个受影响既有用例通过。首轮6通过/1
失败：竞争夹具只改revision而没有事务receipt，生产正确拒绝损坏
账本；夹具改用真正ledger.transition，只重验这一项通过，原FAILED
保留。生产源码在两轮之间未更改。

真实私有CPU flock验证：旧v2无boot run迁移后完整归档并淘汰；持锁/
非STOPPED拒绝；文件读取/身份核验不在三把核心锁持有期间；账本
修订竞争与来源新增拒绝；旧boot证明失效后，原归档支持中断残留
重新核验并resume。未启动、停止、重启或探测模型。

锁路径是专用CPU夹具，包验证替换为该私有锁绑定；不把它宣称为
真实设备空闲或GPU健康证明。实际CLI随包调用仍待组合验证。所有
归档/删除仅CPU夹具，0生产run删除、0模型/0Chat；当前默认/ledger
保持，继续v6可回退交付及其余全部Serve总计划。
''')
(out/'index.json').write_text(json.dumps({str(p.relative_to(out)):dict(bytes=p.stat().st_size,sha256=sha(p)) for p in sorted(out.rglob('*')) if p.is_file()},indent=2)+'\n')
print('epoch migration CPU evidence archived')

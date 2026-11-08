import json
from pathlib import Path
import shutil
import stat
from tools.serve_service.package import read, sha

root = Path.cwd()
base = root / '.q4t-work/serve-progress-20261109'
out = root / 'docs/evidence/serve-progress-discovery-20261109'
out.mkdir(exist_ok=False)
shutil.copyfile(__file__, out / 'archive_discovery.py')
names = ['prepare-01', 'preflight-01', 'discovery-cpu-01', 'business-01/lease']
for name in names:
    for source in (base / name).rglob('*'):
        if not stat.S_ISREG(source.lstat().st_mode):
            continue
        dest = out / source.relative_to(base)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
execution = base / 'business-01/execution'
for source in execution.iterdir():
    if not source.is_file() or source.name.startswith('ledger-'):
        continue
    dest = out / source.relative_to(base)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, dest)
for source in execution.rglob('model.log'):
    dest = out / 'business-01/model-1.log'
    shutil.copyfile(source, dest)
for name in ('plan.json', 'production-rejection.json'):
    shutil.copyfile(base / 'business-01' / name, out / 'business-01' / name)
result = read(execution / 'result.json')
assert result['status'] == 'FAILED' and result['cleanup_failure'] is None
assert result['budget']['counts']['starts'] == result['budget']['counts']['chats'] == 1
assert read(base / 'discovery-cpu-01/result.json')['exit_code'] == 0
assert 'Ran 2 tests' in (base / 'discovery-cpu-01/stderr.log').read_text()
decision = dict(status='PARTIAL_SELF_CHECK_AND_DRIVER_REPAIR',
    actual_business_result='FAILED_DISCOVERY_DRIVER_MISSING_CREDENTIALS',
    model_starts=1, chats=1, seconds=result['seconds'], cleanup_passed=True,
    cpu_regressions_passed=2, default_changed=False, runtime_changed=False,
    cumulative_limits=dict(starts=3, chats=9, probes=1, seconds=900),
    driver_sources={str(p.relative_to(root)): sha(p)
        for p in (root / 'tools/serve_progress').glob('*.py')})
(out / 'decision.json').write_text(json.dumps(decision, indent=2)+'\n')
(out / 'README.md').write_text('''# R03首次真实驱动失败与定点修正

2026-11-09，实际候选1f3d0de4完成1次启动/1条结构化自检，原生ready
及新鲜私有帧正常。45.653秒，驱动读取capabilities漏客户端凭据，
停止并记FAILED。不能把自检当完整恢复/工具验收；原件不追改。

自有停止确认、持久STOPPED/空active、模型进程与端口清理通过。
外层严格租约COMMAND_FAILED对应驱动退出1，无环境中断。默认未改。

仅修改验证驱动：能力GET携带当前代客户端凭据，本机config仍不带
身份头。实际CPU HTTP验证正确头、错误凭据拒绝且不重试；累计预算
保留首轮1启动/1自检，2项通过。未改生产访问控制、模型或私有协议。

后续仍需两代恢复场景，冻结累计3启动/9生成/1探针/900秒，旧时间
和消耗不返还。仅继续未执行的JSON/SSE、停滞恢复、真实文档工具和
观察断连业务；全部Serve目标持续，R03未完成。私有密钥和ledger
绑定凭据不进入此归档；可公开的停止状态、实际观察和计数均保留。
''')
index = {str(p.relative_to(out)): dict(bytes=p.stat().st_size, sha256=sha(p))
    for p in sorted(out.rglob('*')) if p.is_file()}
(out / 'index.json').write_text(json.dumps(index, indent=2)+'\n')
print(json.dumps(dict(files=len(index), preserved_failure=True, cpu_tests=2)))

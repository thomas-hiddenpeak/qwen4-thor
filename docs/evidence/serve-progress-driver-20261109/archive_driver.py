import ast
import json
from pathlib import Path
import shutil
import stat
from tools.serve_service.package import read, sha

root = Path.cwd()
base = root / '.q4t-work/serve-progress-20261109'
out = root / 'docs/evidence/serve-progress-driver-20261109'
out.mkdir(exist_ok=False)
shutil.copyfile(__file__, out / 'archive_driver.py')
for folder in ('driver-cpu-01', 'driver-cpu-02'):
    for path in (base / folder).rglob('*'):
        if not stat.S_ISREG(path.lstat().st_mode):
            continue
        dest = out / path.relative_to(base)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
first = (base / 'driver-cpu-01/stderr.log').read_text()
assert read(base / 'driver-cpu-01/result.json')['exit_code'] == 1
assert first.count(' ... ok') == 4 and first.count(' ... ERROR') == 1
assert read(base / 'driver-cpu-02/result.json')['exit_code'] == 0
assert 'Ran 1 test' in (base / 'driver-cpu-02/stderr.log').read_text()
sources = {}
for path in sorted((root / 'tools/serve_progress').glob('*.py')):
    ast.parse(path.read_text(), filename=str(path))
    sources[str(path.relative_to(root))] = sha(path)
decision = dict(status='PASS_TARGETED_PROGRESS_DRIVER_CPU', different_tests=5,
    first_run_preserved='FAILED_assertion_exception_type', retested_cases=1,
    source_sha256=sources, gpu_starts=0, default_changed=False,
    whole_R03_complete=False, limits=dict(starts=2, chats=8, probes=1, seconds=900))
(out / 'decision.json').write_text(json.dumps(decision, indent=2)+'\n')
(out / 'README.md').write_text('''# R03实际恢复驱动 CPU

2026-11-09，5个不同用例通过；4首轮通过，1生产拒绝负控因驱动误认
异常类型记FAILED。只修正断言，单项补验通过。原失败保留，未重跑
已经通过的4项。没有模型启动、GPU或默认变化。

本机真实CPU进程：ptrace只暂停本组主线程，后台线程继续，撤销后
主线程完成；异常也撤销，旧出生时间/非自有进程在ptrace之前拒绝。
计数先扣再派发且不返还。候选精确摘要只能在独立验证入口接受，
生产发行入口仍拒绝，故障注入/放行代码不进入生产source清单。

真实组仍待执行：2启动/8生成/1恢复CUDA探针/900秒，同一严格租约；
主循环实际超期触发原恢复，旧树清理后新代真实工具业务，再断开
私有观察并确认同PID JSON/SSE及unknown。该CPU证据不代替实际恢复
和正式接受发行，全部Serve总计划继续。
''')
index = {str(p.relative_to(out)): dict(bytes=p.stat().st_size, sha256=sha(p))
    for p in sorted(out.rglob('*')) if p.is_file()}
(out / 'index.json').write_text(json.dumps(index, indent=2)+'\n')
print(json.dumps(dict(files=len(index), different_tests=5, gpu_starts=0)))

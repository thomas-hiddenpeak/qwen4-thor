import json,shutil,stat,subprocess
from pathlib import Path
from tools.serve_service.package import read,sha
root=Path.cwd();base=root/'.q4t-work/serve-progress-20261109';out=root/'docs/evidence/serve-progress-controller-20261109';out.mkdir(exist_ok=False)
shutil.copyfile(base/'archive_controller.py',out/'archive_controller.py')
for folder in ('controller-cpu-01','rollback-verify-01','inventory-02'):
 assert read(base/folder/'result.json')['exit_code']==0
 for p in (base/folder).rglob('*'):
  if not stat.S_ISREG(p.lstat().st_mode):continue
  q=out/p.relative_to(base);q.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,q)
log=(base/'controller-cpu-01/stderr.log').read_text();assert 'Ran 31 tests' in log and log.rstrip().endswith('OK')
paths=set(subprocess.check_output(['git','diff','--name-only'],text=True).splitlines())|set(subprocess.check_output(['git','ls-files','--others','--exclude-standard'],text=True).splitlines());paths={n for n in paths if n.startswith(('src/','include/','tests/','tools/','cmake/')) or n=='CMakeLists.txt'}
assert sha(root/'build/q4t')=='92e600fbcdcadf37271a77405daf498f1fe4fbf2d327ad79a0bf3646bd02ce31'
(out/'decision.json').write_text(json.dumps(dict(status='PASS_PROGRESS_CONTROLLER_TARGETED_CPU',tests_passed=31,source_sha256={n:sha(root/n) for n in sorted(paths)},native_packet_sha256=sha(root/'docs/evidence/serve-progress-native-20261109/integration-cpu-01/work/native-progress.bin'),gpu_starts=0,default_changed=False,whole_R03_complete=False,pending=['PLE/media/service-loop progress','actual bounded recovery business','accepted rollback-capable delivery']),indent=2)+'\n')
(out/'README.md').write_text('''# R03 控制器实际进度与恢复判定 CPU

2026-11-09。31/31针对CPU通过，295来源锚点及5负控通过。本组不改
C++模型计算、不启动GPU、不替换默认。前8原语及10原生接入证据复用。

解码实际C++帧；绑定boot/PID出生时间/命名空间及service/controller/
model代次，严格帧界、时钟、单调完成计数、逐工作身份；每次最多8帧，
有效期2秒。坏帧/断连/积压/观察器异常只使证据unknown。空闲、排队、
背压和辅助等待不等同于执行停滞，其他请求完成不能遮蔽旧任务。

真实工作超过阶段预算与原请求截止30秒宽限，再经至少2秒不同新鲜
快照确认才构成执行合同超期；未知/实际完成/工作或等待改变撤销
候选。恢复继续受旧树清理、独立CUDA探针、连续租约和持久预算约束。
稳定报告要求ready/自检后的实际成功增长及新鲜证据，600秒本身
不再伪称successful_request。不重放模型请求或外部工具。

实际CPU exec通过私有进度FD，许可通道关闭、租约持续；无效辅助FD
与租约别名不阻止核心exec、不关闭租约。v5封装新增进度源码；v1—v4
清单/配置合同通过，当前已交付v4的完整真实包也离线校验通过。

本组仅CPU接入证据，PLE/媒体/服务循环事实、实际故障业务和交付
继续推进，不能作为R03整体完成。全部Serve总计划持续，默认92e600fb
保持，视觉候选保留；无全性能/全模型测试。
''')
index={str(p.relative_to(out)):dict(bytes=p.stat().st_size,sha256=sha(p)) for p in sorted(out.rglob('*')) if p.is_file()};(out/'index.json').write_text(json.dumps(index,indent=2)+'\n');print(json.dumps({'files':len(index),'tests':31,'gpu_starts':0}))

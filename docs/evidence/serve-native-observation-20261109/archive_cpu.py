import json
from pathlib import Path
import shutil
import subprocess
from tools.serve_service.package import read, sha
root=Path.cwd().resolve();base=root/'.q4t-work/serve-native-observation-20261109';out=root/'docs/evidence/serve-native-observation-20261109';out.mkdir(exist_ok=False)
for name in ('native-cpu-01','native-cpu-02'):
 assert read(base/name/'result.json')['exit_code']==0
 shutil.copytree(base/name,out/name)
shutil.copyfile(__file__,out/'archive_cpu.py')
sources=('tools/serve_host/native.py','tools/serve_host/native_exporter.py',
 'tools/serve_host/test_native.py','tools/serve_recovery/bundle.py',
 'tools/serve_recovery/service.py','tools/serve_recovery/acceptance.py',
 'tools/serve_recovery/test_native_bundle.py')
protected={str(root/'build'/n):sha(root/'build'/n) for n in
 ('q4t','q4t.release.json','service.release.json','service-native-state/ledger.json')}
expected=read(root/'.q4t-work/serve-cache-io-20261109/delivery-01/result.json')
assert protected[str(root/'build/q4t')]==expected['engine_binary_sha256']
assert protected[str(root/'build/service-native-state/ledger.json')]==expected['ledger_sha256']
identity=dict(source_base=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
 current_sources={n:sha(root/n) for n in sources},protected=protected,
 result_01_scope='10 CPU cases; after review readiness identity/device failure projection strengthened. Three affected cases rerun in result_02. Unchanged branches reused; result_01 is not claimed to use final source bytes.',
 result_02_scope='Final source: auxiliary/identity projection, actual private status IPC, actual STOPPED default and v5 verification.',
 pending='Actual shipped v6 CLI/export invocation and protected retention; default still accepted v5.')
(out/'identity.json').write_text(json.dumps(identity,indent=2)+'\n')
(out/'decision.json').write_text(json.dumps(dict(status='PASS_NATIVE_OBSERVATION_TARGETED_CPU_PENDING_DELIVERY',
 distinct_cpu_cases=10,affected_reruns=3,gpu_starts=0,chat_requests=0,
 observer_worker_starts_in_timeout_case=3,observer_worker_reaps_in_timeout_case=3,
 actual_loopback_http=True,actual_private_seqpacket=True,current_stopped_bundle_readonly=True,
 legacy_inventory_versions=[1,2,3,4,5],default_changed=False,
 pending=['protected run retention and archive integrity','actual shipped v6 CLI/export invocation','rollback-capable delivery'],
 remaining_master_tasks_open=True),indent=2)+'\n')
(out/'README.md').write_text('''# 当前原生服务观测：针对性CPU检查点

2026-11-09，10个不同CPU检查通过（7个原生观察、3个打包合同），
11.877秒；复核后补完整ready进程身份与DEVICE_FAILURE投影，只重验
3个受影响项，1.131秒。首结果不能冒称使用最终源码字节，未变分支
证据复用与最终来源身份单独记录。

真实私有SEQPACKET仅发status；birth/namespace/目录绑定与新旧模型
代次拒绝、READY_DEGRADED仍可接单/指标缺失不填零、新鲜度/启动后
boot变化、真实HTTP慢连接/方法/头大小/HEAD/未知路由均通过。
真实独立collector被SIGSTOP后4秒超时回收；三个worker启动全部回收，
3/60秒预算挡住第四次，竞争collector未启动。没有模型/Guardian信号。

当前已交付v5包只读采集得到STOPPED，默认ledger/descriptor原件不变；
v1—v5库存保持可核验，新v6包不包含nginx。无GPU启动/生成/性能测试。
尚未完成实际随包CLI/export执行、受保护run留存与默认发行，R04/N06
仍在实施；继续全部Serve总计划，不以本CPU检查点结束。
''')
(out/'index.json').write_text(json.dumps({str(p.relative_to(out)):dict(bytes=p.stat().st_size,sha256=sha(p)) for p in sorted(out.rglob('*')) if p.is_file()},indent=2)+'\n')
print('native CPU evidence archived')

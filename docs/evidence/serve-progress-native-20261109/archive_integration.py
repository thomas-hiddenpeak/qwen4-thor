import json,shutil,stat,subprocess
from pathlib import Path
from tools.serve_service.package import read,sha
root=Path.cwd();base=root/'.q4t-work/serve-progress-20261109';out=root/'docs/evidence/serve-progress-native-20261109';out.mkdir(exist_ok=False)
for name in ('integration-plan.json','integration-required.txt','archive_integration.py'):
 shutil.copyfile(base/name,out/name)
for folder in ('build-03','build-04','integration-cpu-01','inventory-01'):
 assert read(base/folder/'result.json')['exit_code']==0
 for p in (base/folder).rglob('*'):
  if not stat.S_ISREG(p.lstat().st_mode):continue
  q=out/p.relative_to(base);q.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,q)
build=root/'.q4t-work/serve-custom-tools-20261108/engine-build';protocol=read(build/'generated/q4t/server/chat_protocol_sources.json');assert all(sha(root/n)==h for n,h in protocol.items())
paths=set(subprocess.check_output(['git','diff','--name-only'],text=True).splitlines())|set(subprocess.check_output(['git','ls-files','--others','--exclude-standard'],text=True).splitlines());paths={n for n in paths if n.startswith(('src/','include/','tests/','tools/','cmake/')) or n=='CMakeLists.txt'}
(out/'decision.json').write_text(json.dumps(dict(status='PASS_NATIVE_PROGRESS_TARGETED_CPU',tests_passed=10,source_sha256={n:sha(root/n) for n in sorted(paths)},protocol_sources=protocol,binary_sha256=sha(build/'q4t'),host_sha256=sha(build/'q4t_host_tests'),gpu_starts=0,default_changed=False,controller_binding_pending=True,whole_R03_complete=False),indent=2)+'\n')
(out/'README.md').write_text('''# 真实生成进度接入与原生私有采集CPU

2026-11-09。10/10针对CPU通过，实际引擎/host两次零警告构建，来源
294锚点/5负控通过。接入prefill分块、decode/MTP已有完成边界及请求
阶段/等待；调度交接前发布等待、完成归还后恢复，避免并发写者。
缓存/持久写入与执行分开，提交/心跳不冒充完成，不新增CUDA同步。

原生固定二进制帧小于100000字节，显式小端整数，包含实际进程身份、
服务及控制器/模型代次、时间/序号和每请求槽。私有Unix非阻塞发送，
背压/断连只丢观察；不触发SIGPIPE、不阻塞业务。独立采集线程可停止，
错误FD隔离。4上下文/交接、4真实帧/通道、2旧诊断回归通过；实际C++
帧保存于integration-cpu-01/work/native-progress.bin供后续解码验证。

非启动采集的裸serve不增加采集线程。控制器exec传递/消费、身份及
新鲜度决策、稳定业务证明、停滞恢复仍继续接入；原生还未自动恢复。
本组0GPU/0默认替换，不能作为真实模型业务或R03整体完成。下一步
控制器集成及必要的有界故障短验，之后交付并持续全部总计划。
''')
index={str(p.relative_to(out)):dict(bytes=p.stat().st_size,sha256=sha(p)) for p in sorted(out.rglob('*')) if p.is_file()};(out/'index.json').write_text(json.dumps(index,indent=2)+'\n');print(json.dumps({'files':len(index),'candidate':sha(build/'q4t')}))

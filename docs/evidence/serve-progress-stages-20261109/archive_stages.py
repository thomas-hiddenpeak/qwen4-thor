import json,shutil,stat,subprocess
from pathlib import Path
from tools.serve_service.package import read,sha
root=Path.cwd();base=root/'.q4t-work/serve-progress-20261109';out=root/'docs/evidence/serve-progress-stages-20261109';out.mkdir(exist_ok=False)
for name in ('stages-plan.json','stages-required.txt','ple-required.txt','format-only.json','media-test-compiled-source.cpp','archive_stages.py'):
 shutil.copyfile(base/name,out/name)
for folder in ('build-05','build-06','ple-configure-01','ple-build-01','host-cpu-01','ple-cpu-01','media-cpu-01','phase-python-01','inventory-03'):
 code=read(base/folder/'result.json')['exit_code'];assert code==(2 if folder=='build-05' else 0)
 for p in (base/folder).rglob('*'):
  if not stat.S_ISREG(p.lstat().st_mode):continue
  q=out/p.relative_to(base);q.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,q)
for n,count in (('host-cpu-01',11),('ple-cpu-01',4),('media-cpu-01',3)):
 assert f'{count} tests, {count} passed, 0 failed, 0 skipped' in (base/n/'stdout.log').read_text()
assert 'Ran 9 tests' in (base/'phase-python-01/stderr.log').read_text()
assert "No rule to make target 'q4t_progress_media_tests'" in (base/'build-05/stderr.log').read_text()
for n in ('build-05','build-06','ple-build-01'):
 assert 'warning:' not in (base/n/'stdout.log').read_text()+(base/n/'stderr.log').read_text()
build=root/'.q4t-work/serve-custom-tools-20261108/engine-build';protocol=read(build/'generated/q4t/server/chat_protocol_sources.json');assert all(sha(root/n)==h for n,h in protocol.items())
paths=set(subprocess.check_output(['git','diff','--name-only'],text=True).splitlines())|set(subprocess.check_output(['git','ls-files','--others','--exclude-standard'],text=True).splitlines());paths={n for n in paths if n.startswith(('src/','include/','tests/','tools/','cmake/')) or n=='CMakeLists.txt'}
links={str(p.relative_to(root)):subprocess.check_output(['ldd',str(p)],text=True) for p in (base/'ple-build/q4t_ple_health_tests',build/'q4t_progress_media_tests')};assert all('libcuda' not in s and 'libnvidia' not in s for s in links.values());(out/'cpu-linkage.json').write_text(json.dumps(links,indent=2)+'\n')
(out/'decision.json').write_text(json.dumps(dict(status='PASS_PROGRESS_STAGES_TARGETED_CPU',tests_passed=27,groups=dict(host=11,ple=4,media=3,python=9),source_sha256={n:sha(root/n) for n in sorted(paths)},protocol_sources=protocol,binary_sha256=sha(build/'q4t'),host_sha256=sha(build/'q4t_host_tests'),compiled_test_format_only=read(base/'format-only.json'),python_fixture=str(base/'host-cpu-01/work/native-progress.bin'),gpu_starts=0,default_changed=False,whole_R03_complete=False,pending=['actual bounded recovery business','accepted rollback-capable delivery']),indent=2)+'\n')
(out/'README.md').write_text('''# R03 实际子阶段与服务循环 CPU

2026-11-09。27/27针对CPU通过：11上下文/嵌套/循环/实际帧，4真实
io_uring合成文件成功/短读/失败及旧上传归属，3实际图像/视频处理
完成与失败，9 v1/v2跨语言解码与期限/媒体/循环判定。像素逐位保持，
不改计算、不增加CUDA同步；纯CPU helper未链接CUDA/NVML，见linkage。

接入循环自己记录每轮实际完成，独立发布线程的快照序号不能代替；
循环30秒单轮预算加30秒宽限和2秒新鲜确认。PLE/媒体固定、无分配、
无I/O的线程局部回调保留外层工作身份，已完成读取波次和CPU变换才
计数，视觉只在已有设备完成边界计数；前一个请求的上传不计到当前。
失败、scope退出与提交不计完成。补grammar/storage等待原因。

候选引擎1f3d0de4完成零警告构建，来源296锚点与5负控通过。首次
build-05已完成引擎/host，但旧make入口在自动再生成后仍未识别新增
媒体目标，整体退出2；原FAILED完整保留。build-06重新进入已生成
入口通过，未改运行时源码或掩盖失败。媒体测试随后仅按80列换行，
前后去空白完全一致；编译时源码及等价摘要保留，不为排版重复测试。

本组0GPU/0默认替换。当前默认92e600fb和视觉候选保留；接着按既定
2启动/8生成/900秒上限完成实际控制循环停滞、受控恢复和恢复后业务，
再可回退发行。R03尚未完成，全部Serve总目标继续。
''')
index={str(p.relative_to(out)):dict(bytes=p.stat().st_size,sha256=sha(p)) for p in sorted(out.rglob('*')) if p.is_file()};(out/'index.json').write_text(json.dumps(index,indent=2)+'\n');print(json.dumps({'files':len(index),'candidate':sha(build/'q4t'),'tests':27}))

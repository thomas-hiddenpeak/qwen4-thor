import json
from pathlib import Path
import shutil
from tools.serve_service.package import read,sha
root=Path.cwd().resolve();base=root/'.q4t-work/serve-cache-io-20261109';out=root/'docs/evidence/serve-cache-progress-delivery-20261109';out.mkdir(exist_ok=False)
paths=['prepare_delivery.py','install_delivery.py','archive_delivery.py','release-01/decision.json','release-01/control-review.json','release-01/engine/q4t.release.json','release-01/engine/runtime-source.json','release-01/native-build.json','release-01/receipt.json','release-01/source-snapshot.json','release-01/bundle/manifest.json','release-01/bundle/engine/manifest.json','delivery-01/rollback/receipt.json']
for folder in ('package-01','package-02','install-01','delivery-01'):
 paths.extend(str(p.relative_to(base)) for p in (base/folder).iterdir() if p.is_file())
for name in sorted(set(paths)):
 p=base/name;q=out/name;q.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,q)
r=read(base/'delivery-01/result.json');assert r['status']=='PASS_NATIVE_PROGRESS_CACHE_DEFAULT_INSTALL'
for f,k in [('q4t','engine_binary_sha256'),('q4t.release.json','engine_release_sha256'),('service.release.json','service_release_sha256')]:assert sha(root/'build'/f)==r[k]
assert sha(root/'build/service-native-state/ledger.json')==r['ledger_sha256']
assert read(base/'package-01/result.json')['exit_code']==1
assert read(base/'package-02/result.json')['exit_code']==0
(out/'README.md').write_text('''# 原生进度恢复与缓存隔离默认交付

2026-11-09。引擎1ff729a0/元数据f44634f5，原生v5包fac58f34/服务
发布1370792e进入默认。698冻结来源、208嵌入协议来源及10组CPU/实际
业务/旧发行证据逐文件核验；R03实际主循环恢复、观察失联同PID工具
业务，缓存worker故障与新代恢复、MTP缓存SSE组合短验均已通过。
后续唯一控制源差异为cache/resource work=4预算，直接CPU及实际等待
判定覆盖；原生ingress/Guardian/CUDA探测ELF不变并复用原接受证据。

发行保留真实缓存短验configuration：max_len16384、缓存启用；默认
托管包仍按已验R03的8192/cache-off配置。前者没有篡改成后者或伪称
默认运行采集。应用存储限额相同，已有Chat/Responses/Conversations
验收按未变依赖复用。prepare的recovery_accepted=false表示封装动作
本身不产生验收；独立decision及service.release记录实际接受依据。

首封装在读取旧证据索引时因包装格式不同而失败，未创建包或改变
默认；修复读取后封装成功，原失败/生成脚本保留。安装持有GPU/
Guardian/writer锁，4份旧默认文件与旧服务包完整保留，回退回执
8ccbd366。ledger2216b9dc、service_id、Key/CIDR/配额均不变。
服务维持STOPPED，仅离线/help/status检查，0模型启动/0Chat；UNKNOWN
表示无活动运行快照，不声称正在服务。无额外GPU/完整性能或长稳。

R01/R03本组交付完成后继续R04原生观测/受保护留存及其余全部总任务，
并未把局部交付当完整OpenAI兼容或总目标完成。二进制、完整旧包和
私有回退文件保留在本机，仓库保存来源/原件摘要，不复制凭据。
''')
index={str(p.relative_to(out)):dict(bytes=p.stat().st_size,sha256=sha(p)) for p in sorted(out.rglob('*')) if p.is_file()};(out/'index.json').write_text(json.dumps(index,indent=2)+'\n')
print(json.dumps(dict(files=len(index),result=r)))

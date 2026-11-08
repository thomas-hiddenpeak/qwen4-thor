import json,shutil
from pathlib import Path
from tools.serve_service.package import read,sha
root=Path.cwd();src=root/'.q4t-work/serve-chat-storage-20261109';out=root/'docs/evidence/serve-chat-delivery-20261109';out.mkdir(exist_ok=False)
paths=['prepare_delivery.py','install_delivery.py','archive_delivery.py','release-01/decision.json','release-01/control-review.json','release-01/engine/q4t.release.json','release-01/engine/runtime-source.json','release-01/native-build.json','release-01/receipt.json','release-01/source-snapshot.json','release-01/bundle/manifest.json','release-01/bundle/engine/manifest.json','delivery-01/rollback/receipt.json']
for folder in ('package-01','install-01','delivery-01'):
 paths.extend(str(p.relative_to(src)) for p in (src/folder).iterdir() if p.is_file())
for name in sorted(set(paths)):
 p=src/name;q=out/name;q.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,q)
r=read(src/'delivery-01/result.json');assert r['status']=='PASS_STORED_CHAT_DEFAULT_INSTALL';assert sha(root/'build/q4t')==r['engine_binary_sha256'] and sha(root/'build/q4t.release.json')==r['engine_release_sha256'];assert sha(root/'build/service.release.json')==r['service_release_sha256'];assert sha(root/'build/service-native-state/ledger.json')==r['ledger_sha256']
(out/'README.md').write_text('''# Chat持久能力与公开故障语义默认交付

2026-11-09。实际验收引擎92e600fb、协议7cf94e90进入默认；元数据
 dfffcbad，原生包ffbd9d4b、服务发布e4d8b321。717项冻结来源和所有
复用原件逐文件核验；实际生成/故障短验与针对性CPU直接覆盖新行为。
完整存储JSON/SSE多choice/logprobs、元数据/列表/原消息、真实资料工具
与回填JSON通过；未知提交错误准确、可查原ID，同PID继续提供服务。
两次原FAILED保持原结论，修复后只续对应失败项。

原生ingress cc35816e已在实际路由/TLS/CORS CPU验证，唯一变化的
控制源为可选nginx应用路由，已有真实nginx验证；Guardian/CUDA探测
及恢复策略不变，旧Responses/Conversations验收按不变依赖复用。
发行v3声明携带新Chat应用合同及OpenAPI，离线校验通过。

安装持有GPU/Guardian/writer锁，保留上版引擎c704eebf、元数据3216d5af、
服务包d0e5f43f及四份回退文件；回退回执478c7785。Key/CIDR/配额和
service_id不变，ledger摘要2216b9dc保持、意图STOPPED。安装仅help/status
检查，0 GPU启动/0 Chat，没有自动启动。status UNKNOWN表示没有活动
运行快照，并未把停止服务描述成正在服务。

ELF与旧包在本机私有发行/回退目录保留，仓库保存来源及摘要。
下一步继续R03实际完成进度/忙等待卡死分类、R04观测与其余全部总计划。
A07/A08尚有治理及高级执行器，未作完整OpenAI兼容、性能或长稳通过主张。
''')
index={str(p.relative_to(out)):{'bytes':p.stat().st_size,'sha256':sha(p)} for p in sorted(out.rglob('*')) if p.is_file()};(out/'index.json').write_text(json.dumps(index,indent=2)+'\n');print(json.dumps({'files':len(index),'result':r}))

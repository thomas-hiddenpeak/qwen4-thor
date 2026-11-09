import hashlib,json,shutil,subprocess
from pathlib import Path
root=Path.cwd().resolve();base=root/'.q4t-work/serve-access-20261109';out=root/'docs/evidence/serve-access-native-20261109';out.mkdir()
sha=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest()
for name in ('build-04','native-cpu-01'):shutil.copytree(base/name,out/name)
shutil.copyfile(__file__,out/'archive_native.py')
files=['cmake/NativeIngress.cmake','tests/ingress_access_test.cpp','tools/serve_recovery/access_control.py','tools/serve_recovery/test_access_ingress.py','tools/serve_recovery/bundle.py']+['include/q4t/service/'+n+'.h' for n in ('ingress_access','ingress_access_admin','ingress_admin')]+['src/service/'+n+'.cpp' for n in ('ingress_access','ingress_access_admin','ingress_admin','ingress_main')]
(out/'identity.json').write_text(json.dumps(dict(base_commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),sources={n:sha(root/n) for n in files},default={n:sha(root/'build'/n) for n in ('q4t','q4t.release.json','service.release.json','service-native-state/ledger.json')},native_build_receipt_inventory_extended_after_tests=True),indent=2)+'\n')
(out/'decision.json').write_text(json.dumps(dict(status='PASS_NATIVE_ACCESS_PUBLICATION_TARGETED_CPU_PENDING_MANAGED_INTEGRATION',cpp_new_tests=2,python_native_tests=6,publisher_identity_test=1,build_warnings=0,native_tls_in_this_build=False,cpu_backend_only=True,model_starts=0,real_chat_generations=0,default_changed=False,remaining=['isolated durable I/O helper and recovery bounds','managed CLI/package integration','dynamic quotas and restart continuity','actual model business and rollback-capable delivery'],remaining_master_tasks_open=True),indent=2)+'\n')
(out/'README.md').write_text('''# N04真实原生入口认证与私有发布CPU

2026-11-09，2个新增C++追赶合同、6个原生HTTP场景及1个Publisher
身份/新鲜度测试通过，独立host原生入口零警告构建。入口候选
6ad08246，本CPU构建TLS关闭；默认已验TLS包未变。

真实SO_PEERCRED、私有SEQPACKET分段和TCP HTTP：无策略503到完整
发布；重叠轮换/撤销；在途SSE保持原身份且新Key仍占同一客户端
容量；取消请求传递相同身份；同UID非父进程可读状态但发布拒绝；
4096条历史大清单、坏长度和慢消息不替换策略；重启先503、恢复最新
撤销后旧Key仍401；同revision不同摘要拒绝；旧配置及伪造身份头
合同保持。Publisher不能沿用其他PID或陈旧状态宣称live_applied。

HTTP上游是明确的CPU夹具，不是模型；不把身份头传递当完整模型
取消/缓存/会话隔离验收。持久I/O隔离/管理CLI/额度协同和真实模型
闭环继续实施。native构建来源清单加入新认证/管理和SHA256依赖，
包清单组合待托管集成统一检查，未把该元数据编辑冒称已测试。

原件不包含私有配置和Key。默认v6 b7c25107、引擎1ff729a0、ledger
保持，0模型/真实Chat/设备探针。N04和全部Serve总任务仍未完成。
''')
(out/'index.json').write_text(json.dumps({str(p.relative_to(out)):dict(bytes=p.stat().st_size,sha256=sha(p)) for p in sorted(out.rglob('*')) if p.is_file()},indent=2)+'\n');print(len(json.loads((out/'index.json').read_text())))

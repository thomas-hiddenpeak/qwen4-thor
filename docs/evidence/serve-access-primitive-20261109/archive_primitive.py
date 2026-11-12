import hashlib,json,shutil,subprocess
from pathlib import Path
root=Path.cwd().resolve();base=root/'.q4t-work/serve-access-20261109';out=root/'docs/evidence/serve-access-primitive-20261109';out.mkdir()
sha=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest()
for name in ('build-01','build-02','primitive-cpu-01'):shutil.copytree(base/name,out/name)
for name in ('build_primitive.py','archive_primitive.py'):shutil.copyfile(base/name,out/name)
(out/'identity.json').write_text(json.dumps(dict(base_commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),default={n:sha(root/'build'/n) for n in ('q4t','q4t.release.json','service.release.json','service-native-state/ledger.json')},candidate_native_auth_not_yet_wired=True),indent=2)+'\n')
(out/'decision.json').write_text(json.dumps(dict(status='PASS_NATIVE_ACCESS_SNAPSHOT_PRIMITIVE_PENDING_INTEGRATION',targeted_tests=6,build_warnings=0,model_starts=0,chat_requests=0,default_changed=False,initial_configuration_failure='Missing OpenSSL development headers in default include path. Independent non-TLS access primitive built with Q4T_ENABLE_NATIVE_TLS=OFF; delivered TLS-enabled native entrypoint unchanged.',remaining=['durable transactions and native management','actual entrypoint integration','dynamic quotas and continuity','targeted real business and rollback-capable delivery'],remaining_master_tasks_open=True),indent=2)+'\n')
(out/'README.md').write_text('''# N04 原生认证快照首组CPU

2026-11-09，独立host构建零警告，6/6直接CPU通过。覆盖新旧Key重叠、
撤销及已准入身份保持；坏格式/revision冲突原子拒绝；稳定槽位和
历史Key不可转授；重建后保留撤销历史；4线程与100次发布交错；
64客户端/4活跃Key/4096历史指纹边界以及状态不泄露Key/单Key摘要。
指纹使用项目已有SHA256，无新增外部依赖、文件I/O或模型权限。

首次configure因默认路径缺OpenSSL开发头失败，原件保留。该原语
不调用TLS，以关闭TLS构建选项生成独立CPU目标；没有替换既有TLS
入口，也没有把它当TLS验收。引擎1ff729a0、v6发行b7c25107与ledger
保持；0模型/Chat。原语尚未接到实际认证路径和持久管理，不能称
密钥轮换已经可用交付。继续事务、入口、动态限额、配额连续性和
真实组合，N04及全部总任务均未完成。
''')
(out/'index.json').write_text(json.dumps({str(p.relative_to(out)):dict(bytes=p.stat().st_size,sha256=sha(p)) for p in sorted(out.rglob('*')) if p.is_file()},indent=2)+'\n')
print(len(json.loads((out/'index.json').read_text())))

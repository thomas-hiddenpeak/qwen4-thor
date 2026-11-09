import hashlib,json,shutil,subprocess
from pathlib import Path
root=Path.cwd().resolve();base=root/'.q4t-work/serve-access-20261109';out=root/'docs/evidence/serve-access-store-20261109';out.mkdir()
sha=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest()
for name in ('build-03','store-cpu-01','store-cpu-02'):shutil.copytree(base/name,out/name)
shutil.copyfile(__file__,out/'archive_store.py')
files=['tools/serve_recovery/'+n for n in ('access_policy.py','access_store.py','test_access_store.py')]+['tests/ingress_access_test.cpp']
(out/'identity.json').write_text(json.dumps(dict(base_commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),sources={n:sha(root/n) for n in files},default={n:sha(root/'build'/n) for n in ('q4t','q4t.release.json','service.release.json','service-native-state/ledger.json')}),indent=2)+'\n')
(out/'decision.json').write_text(json.dumps(dict(status='PASS_ACCESS_POLICY_JOURNAL_TARGETED_CPU_PENDING_LIVE_INTEGRATION',first_tests=8,review_added_test=1,affected_review_tests=3,distinct_tests=9,build_warnings=0,native_python_contracts_verified=True,history_bound=4096,receipt_bound=128,recovery_reaffirms_file_and_directory_fsync=True,live_applied_claimed=False,default_changed=False,model_starts=0,chat_requests=0,remaining_master_tasks_open=True),indent=2)+'\n')
(out/'README.md').write_text('''# N04私有策略事务与未知提交恢复CPU

2026-11-09，首组8/8通过，复核补1个恢复durability用例后3个直接
受影响项通过，共9个不同Python用例。C++互通checker按新增stdin
验证入口重建零警告，先前6个C++原语逻辑未改、证据直接复用。

覆盖Python/C++同一语义摘要（包括4096条历史、大于128KiB输入）；
私有真实文件与幂等；rename前完整pending一次恢复；rename后fsync
结果未知可按原事务查询，不重复更新；恢复时重新fsync文件/目录，
存储仍失败不声明durable；损坏/缺失不重新从旧包引入Key；竞争/外来
身份/链接拒绝；128回执滚动有界，旧请求不能重置revision；头记录
变化/错误pending前驱拒绝。没有改动现有Guardian journal，复用其
已有的恢复后再次持久化原则。

持久结果始终live_applied=false，不能当成真实入口已换Key。文件仅
专属CPU目录，无生产策略或服务操作，原件不含私有策略或Key。
默认v6 b7c25107/引擎1ff729a0/ledger保持，0模型/Chat。继续隔离I/O
控制器、原生管理及实际认证接入、动态额度和真实组合；全部总目标
继续，N04尚未完成。
''')
(out/'index.json').write_text(json.dumps({str(p.relative_to(out)):dict(bytes=p.stat().st_size,sha256=sha(p)) for p in sorted(out.rglob('*')) if p.is_file()},indent=2)+'\n');print(len(json.loads((out/'index.json').read_text())))

import json,shutil,stat,fcntl,os
from pathlib import Path
from jsonschema import Draft202012Validator
from tools.serve_chat_storage.final_acceptance import accepted_tool_prefix,CORRECTED
from tools.serve_chat_storage.acceptance import check_fault,shape
from tools.serve_service.package import read,sha
root=Path.cwd();src=root/'.q4t-work/serve-chat-storage-20261109';latest=src/'business-03'
accepted_tool_prefix(src/'business-02');plan=read(latest/'plan.json');result=read(latest/'result.json');lease=read(src/'lease-03/exit.json');clean=read(latest/'cleanup.json')
assert result['status']=='PASS_STORED_CHAT_CORRECTED_FAULT_AND_RECOVERY' and lease['status']=='COMPLETED' and lease['child_returncode']==0
assert result['counts']['starts']==1 and result['counts']['chats']==2 and result['seconds']<300
assert clean['model_exit']==0 and not clean['remaining'] and clean['ports_released'] and clean['lease_continuously_held']
assert sha(plan['binary'])==plan['binary_sha256']==result['binary_sha256']
assert all(sha(root/n)==h for n,h in plan['sources'].items())
assert all(sha(n)==h for n,h in plan['protected'].items())
check_fault(read(latest/'fault.events.json'),(latest/'fault.wire').read_bytes())
shape(read(latest/'same-pid-chat.sdk.json')['result'],'stored_chat')
shape(read(latest/'delete.sdk.json')['result'],'stored_chat_deleted')
configs=0;schema=read(root/'docs/serve-config-v1.schema.json');v=Draft202012Validator(schema)
for file in sorted(latest.glob('*config*.json')):
 value=read(file)
 if 'protocol_source_sha256' in value:v.validate(value);configs+=1
attempts=[];queries=[]
for index in (1,2,3):
 business=src/f'business-{index:02d}';log=(business/'model-1.log').read_text();row=read(business/'result.json');cl=read(business/'cleanup.json')
 assert cl['model_exit']==0 and not cl['remaining'] and cl['ports_released']
 final=[json.loads(line[len('[startup-reservation] '):]) for line in log.splitlines() if line.startswith('[startup-reservation] ') and '"event":"finalized"' in line]
 assert len(final)==1 and final[0]['allocated']==final[0]['freed']==1606 and final[0]['release_unknown']==0
 q=[json.loads(line) for line in (src/f'lease-{index:02d}'/'nvml-queries.jsonl').read_text().splitlines()]
 assert q and all(x['status']=='OK' and not x['observed_errors'] for x in q)
 assert not q[0]['pids'] and not q[-1]['pids'];queries.extend(q)
 attempts.append(dict(index=index,status=row['status'],counts=row['counts'],seconds=row['seconds'],nvml_samples=len(q),cleanup_confirmed=True))
fd=os.open(src.parent/'gpu-exclusive.lock',os.O_RDWR);fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);fcntl.flock(fd,fcntl.LOCK_UN);os.close(fd)
counts={k:sum(a['counts'][k] for a in attempts) for k in ('starts','chats','controls')};seconds=sum(a['seconds'] for a in attempts)
assert counts['starts']==3 and counts['chats']==8 and seconds<600
out=root/'docs/evidence/serve-chat-business-20261109';out.mkdir(exist_ok=False)
for folder in ('business-03','lease-03'):
 for p in sorted((src/folder).rglob('*')):
  if not stat.S_ISREG(p.lstat().st_mode) or p.name=='.q4t-lock':continue
  q=out/p.relative_to(src);q.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,q)
shutil.copyfile(src/'archive_business.py',out/'archive_business.py')
decision=dict(status='ACCEPT_TARGETED_STORED_CHAT',binary_sha256=result['binary_sha256'],protocol_sha256=read(latest/'capabilities-1.json')['protocol_source_sha256'],source_commit=plan['source_commit'],sources=len(plan['sources']),attempts=attempts,counts=counts,seconds=seconds,all_nvml_ok=True,nvml_samples=len(queries),lease_status=lease['status'],config_schema_samples=configs,original_failures_preserved=True,accepted_prior_normal_prefix='serve-chat-storage-prefix-20261109',accepted_prior_tool_prefix='serve-chat-unknown-20261109',corrected_sources=sorted(CORRECTED),corrected_fault=True,same_model_continued=True,all_allocations_released=True,all_ports_and_locks_released=True,default_changed=False,performance_acceptance=False,long_stability_accepted=False,master_tasks_remaining=True)
(out/'decision.json').write_text(json.dumps(decision,ensure_ascii=False,indent=2)+'\n')
(out/'README.md').write_text(f'''# Chat保存及未知提交隔离真实业务组合验收

2026-11-09。最终补验1启动/2Chat/{result['seconds']:.3f}秒通过，严格租约
COMPLETED/child0。暂停精确自有存储worker后，原生恢复回收worker；
实际SSE返回object_commit_unknown及原completion_id，无成功终态，
同PID模型随后MTP回答42。此前持久对象在跨进程及worker恢复后仍一致，
删除/404有效，计数1成功1失败、准入/worker资源归零。

累计3启动/8Chat/{seconds:.3f}秒，其中两个原FAILED不追改：首轮SDK
分页驱动误读和第二轮公开错误码缺陷。正常JSON/SSE多choice/logprobs
落盘、真实项目资料工具/回填JSON、原历史及metadata通过证据按不变
依赖复用；最终只重验修正错误出口和同PID业务。4生产文件变化均为
公开错误投影，相关5CPU已通过；没有重跑无关推理。

{len(queries)}次NVML全部OK；每次模型退出0、1606启动分配全释放、拥有进程/
端口/锁清理。最终配置{configs}份严格匹配schema。最新候选/协议身份见
decision.json，默认仍保留上版，接着可回退发行及全部Serve余项。
这是针对性业务验收，不作全模型质量、性能或用户长稳通过主张。
''')
index={str(p.relative_to(out)):{'bytes':p.stat().st_size,'sha256':sha(p)} for p in sorted(out.rglob('*')) if p.is_file()};(out/'index.json').write_text(json.dumps(index,indent=2)+'\n')
print(json.dumps(decision,ensure_ascii=False))

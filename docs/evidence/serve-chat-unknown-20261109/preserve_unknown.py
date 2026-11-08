import json,shutil,stat
from pathlib import Path
from tools.serve_chat_storage.final_acceptance import accepted_tool_prefix,CORRECTED
from tools.serve_service.package import read,sha
root=Path.cwd();src=root/'.q4t-work/serve-chat-storage-20261109';old=src/'business-02'
out=root/'docs/evidence/serve-chat-unknown-20261109';out.mkdir(exist_ok=False)
accepted_tool_prefix(old)
changed={name for name,digest in read(old/'plan.json')['sources'].items() if sha(root/name)!=digest}
assert changed==CORRECTED,changed
for folder in ('business-02','lease-02','build-06','build-07','unknown-cpu-01','unknown-inventory-01'):
 for p in sorted((src/folder).rglob('*')):
  if not stat.S_ISREG(p.lstat().st_mode) or p.name=='.q4t-lock':continue
  q=out/p.relative_to(src);q.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,q)
for name in ('unknown-required.txt','unknown-fix-plan.json','tool-prefix-review.json','preserve_unknown.py'):
 shutil.copyfile(src/name,out/name)
queries=[json.loads(line) for line in (src/'lease-02/nvml-queries.jsonl').read_text().splitlines()]
review=dict(original_failed=read(old/'result.json'),lease=read(src/'lease-02/exit.json'),accepted_prefix_only=True,source_delta=sorted(changed),cpu=read(src/'unknown-cpu-01/result.json'),nvml_query_count=len(queries),final_driver_sha256=sha(root/'tools/serve_chat_storage/final_acceptance.py'),candidate_sha256=sha(root/'.q4t-work/serve-custom-tools-20261108/engine-build/q4t'),default_changed=False)
(out/'review.json').write_text(json.dumps(review,ensure_ascii=False,indent=2)+'\n')
(out/'README.md').write_text('''# Chat真实未知提交缺陷、修复与针对性CPU原件

2026-11-09。第二轮1启动/3条Chat/53.830秒，真实资料工具SSE、读取本项目
文档、回填严格JSON、保存完整工具历史及metadata更新通过。精确暂停自有
存储worker后，生成完成但落盘回执超时，内部commit_unknown没有传播至
公开错误码，实际返回object_timeout。这是实际缺陷，原FAILED与租约
COMMAND_FAILED/child1原样保留；不把通过前缀视为整组通过。

修复统一Chat、Responses及Conversations HTTP出口：内部原因不丢失，
未知写入返回object_commit_unknown，并带原资源ID查询提示；不自动重放。
5/5直接CPU通过，覆盖真实提交后丢回执、JSON/SSE错误、不伪造成功终态、
会话创建原ID查证、已知提交前取消和worker恢复预算。两次构建零警告，
最后一次只有行宽调整；来源290锚点/5负控通过。所有原始记录及索引保留。

只补验纠正后的故障和同PID普通Chat：1启动/2Chat/300秒，累计3启动/
8Chat/600秒。此前通过的3正常生成、2工具生成和相关管理操作按未改依赖
复用。原状态私有复制，不改原件；默认未更换，之后继续全部Serve总计划。
''')
index={str(p.relative_to(out)):{'bytes':p.stat().st_size,'sha256':sha(p)} for p in sorted(out.rglob('*')) if p.is_file()}
(out/'index.json').write_text(json.dumps(index,indent=2)+'\n');print(json.dumps(dict(files=len(index),candidate=review['candidate_sha256'],source_delta=sorted(changed))))

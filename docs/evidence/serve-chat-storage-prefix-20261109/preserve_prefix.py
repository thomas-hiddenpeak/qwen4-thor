import hashlib,json,shutil,stat
from pathlib import Path
from tools.serve_chat_storage.remainder_acceptance import prefix,CHANGED_DRIVER
from tools.serve_service.package import read,sha
root=Path.cwd();source=root/'.q4t-work/serve-chat-storage-20261109';previous=source/'business-01';out=root/'docs/evidence/serve-chat-storage-prefix-20261109';out.mkdir(exist_ok=False)
stored,stream=prefix(previous);plan=read(previous/'plan.json');changed={name for name,digest in plan['sources'].items() if sha(root/name)!=digest};assert changed==CHANGED_DRIVER
for folder in ('business-01','lease-01','bridge-cpu-02'):
 for p in sorted((source/folder).rglob('*')):
  if not stat.S_ISREG(p.lstat().st_mode) or p.name=='.q4t-lock':continue
  q=out/p.relative_to(source);q.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,q)
queries=[json.loads(line) for line in (source/'lease-01/nvml-queries.jsonl').read_text().splitlines()]
review=dict(original_result=read(previous/'result.json'),lease=read(source/'lease-01/exit.json'),prefix_revalidated=True,accepted_original_requests=['unretained','json','stream'],bridge_failure='official SDK CursorPage does not expose raw first_id/last_id; original wire does contain both',source_delta=sorted(changed),nvml_query_count=len(queries),next_limits=dict(starts=1,chats=4,seconds=300),cumulative_limits=dict(starts=2,chats=7,seconds=600),default_changed=False)
(out/'prefix-review.json').write_text(json.dumps(review,ensure_ascii=False,indent=2)+'\n')
shutil.copyfile(source/'preserve_prefix.py',out/'preserve_prefix.py')
(out/'README.md').write_text('''# Chat持久首轮原件与仅剩余续验

2026-11-09。首轮1启动/3条Chat/42.497秒，在列表验收驱动误读SDK分页
对象处失败，原FAILED与租约COMMAND_FAILED/child1保留。引擎8fbb50e9
未改变；实际原始列表具有first_id/last_id，SDK CursorPage没有直接
暴露两者。已改桥接从同一被捕获的正文保留字段，并核对SDKdata；
针对该案例的CPU桥接补验通过，无多请求或第二正文消费者。

已完成三条真实请求及其GET证据重新从原件核对：不保存/metadata、
双choice JSON、双choice SSE及真实概率/原bytes/usage与持久值一致，
真实算术回答42。列表原始wire严格通过schema。该子集有业务证据，
不能把首轮整体或完整Chat存储标为通过。模型退出0、1606分配全部
释放、拥有进程/端口清理；租约释放，没有干预其他服务。

只续验未执行项：私有复制原持久状态，1次启动/4条Chat/300秒，
完成列表、实际工具及回填JSON、原消息、metadata、存储故障与同PID
Chat、原对象恢复后读取/删除。累计保持7条Chat，不重做前三条生成；
累计最多2启动/600秒。默认保留，完成后继续全部总计划。
''')
index={str(p.relative_to(out)):{'bytes':p.stat().st_size,'sha256':sha(p)} for p in sorted(out.rglob('*')) if p.is_file()};(out/'index.json').write_text(json.dumps(index,indent=2)+'\n');print('preserved files',len(index))

import hashlib,json,shutil,stat,subprocess
from pathlib import Path
root=Path.cwd(); source=root/'.q4t-work/serve-chat-storage-20261109'; out=root/'docs/evidence/serve-chat-storage-20261109'
out.mkdir(exist_ok=False)
for name in ['core-plan.json','core-required.txt','integration-plan.json','integration-required.txt','query-fix-plan.json','query-required.txt','archive_cpu.py']:
 shutil.copyfile(source/name,out/name)
for folder in ['build-01','build-02','build-03','build-04','build-05','core-cpu-01','integration-cpu-01','query-cpu-01','sdk-01','sdk-02','sdk-03','declarations-01','inventory-01','native-01','tls-01','nginx-01']:
 for path in sorted((source/folder).rglob('*')):
  mode=path.lstat().st_mode
  if not stat.S_ISREG(mode):continue
  rel=path.relative_to(source)
  if folder in ('native-01','tls-01','nginx-01') and len(rel.parts)>2:continue
  if path.name.startswith('.q4t-') or path.name=='ready.json':continue
  target=out/rel;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(path,target)
changed=subprocess.check_output(['git','diff','--name-only'],text=True).splitlines()
new=subprocess.check_output(['git','ls-files','--others','--exclude-standard'],text=True).splitlines()
paths=sorted(set(p for p in changed+new if p.startswith(('src/','include/','tests/','tools/','cmake/')) or p=='CMakeLists.txt'))
summary={'base_commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'source_sha256':{p:hashlib.sha256((root/p).read_bytes()).hexdigest() for p in paths},'cpu':{'core':15,'integration':9,'query_fix':4,'sdk_distinct_scenarios':4,'native_ingress_tls_optional_nginx':3,'declarations':4},'wire_shapes':json.loads((source/'declarations-01/work/wire-shape-counts.json').read_text()),'preserved_failures':{'build-02':'missing OutputJson declaration; fixed include','sdk-01':'fixture detail low outside current media contract; same test auto and original field retention continued','sdk-02':'actual nullable metadata query rejected; fixed production parser and malformed UTF-8 validation'},'build_03_note':'q4t and host built; output filename q4t-ingress did not build CMake q4t_ingress target; actual ingress built separately in build-04','gpu_starts':0,'default_changed':False,'fixture_private_keys':'remain in local work directory; not archived'}
(out/'decision.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
(out/'README.md').write_text('''# Chat持久对象、实际协议与入口 CPU 验收

2026-11-09。核心15、接入9、查询补验4项C++均通过；其中重验只覆盖
实际变化和直接受影响项。四个官方SDK场景通过（首次三项复用，分页
在sdk-03通过），25个真实CPU HTTP资源通过schema：14 completion、
1 deleted、5列表及5原始消息页。声明4项与来源290锚点/5负控通过。

实际原生入口/独立控制准入、TLS/CORS和可选nginx共3项通过。
可选nginx同时补齐此前漏转发的Responses与Conversations；默认仍原生。
引擎/host及实际q4t_ingress构建零警告。build-02缺声明失败保留；
build-03的输出文件名不是CMake目标，实际入口由build-04重新构建。

sdk-01分页夹具误用了当前媒体合同以外的detail=low，改auto保留字段
保真范围，低/高细节仍属后续媒体任务。sdk-02暴露真正SDK nullable
metadata=兼容缺口，已修复，顺带拒绝损坏UTF-8和混合null/object查询。
首次失败原件不改结论；三项未受影响SDK不重复，最终来源见decision。
存储CRUD、配额、丢回执、期限核心未改行为复用core-cpu-01，变动查询
则执行针对性补验。运行目录仅归档普通文件，不归档socket/锁和测试私钥。

本组无GPU模型或默认替换；SDK创建输出明确是CPU夹具，不能算真实
模型质量。下一步一次模型启动下的保存/查询/工具闭环与存储故障隔离，
通过后可回退发行，继续全部主计划，不能把本组当A08或总目标完成。
''')
index={str(p.relative_to(out)):{'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted(out.rglob('*')) if p.is_file()}
(out/'index.json').write_text(json.dumps(index,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'archive':str(out),'files':len(index),'bytes':sum(row['bytes'] for row in index.values())}))

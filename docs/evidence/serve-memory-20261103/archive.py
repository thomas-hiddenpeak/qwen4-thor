from pathlib import Path
import json
import shutil
import sys
ROOT = next(p for p in Path(__file__).resolve().parents if (p / 'AGENTS.md').is_file())
sys.path.insert(0,str(ROOT))
from tools.serve_memory import stage
h,base=stage.h,stage.BASE
destination=ROOT/'docs/evidence/serve-memory-20261103'
h.require(not destination.exists(),'preserve existing archive')
for folder in (stage.PACKAGE,stage.OUTPUT): h.verify_seal(folder)
summary=h.read(base/'acceptance-summary.json')
h.require(summary['business']['status']=='PASS_BOUNDED_MEMORY_ADMISSION','acceptance missing')
destination.mkdir()
sources,local={},{}
paths=[p for folder in (stage.PACKAGE,stage.OUTPUT) for p in folder.rglob('*') if p.is_file()]
paths += [base/name for name in ('archive.py','summarize.py','postflight-gpu.json','postflight.json','acceptance-summary.json','memory-summary.json')]
for path in sorted(paths):
 relative=str(path.relative_to(base)); record=h.identity(path)
 with path.open('rb') as f: magic=f.read(4)
 if magic==b'\x7fELF' or path.suffix in ('.o','.a','.bundle'):
  local[relative]=record; continue
 target=destination/relative; target.parent.mkdir(parents=True,exist_ok=True)
 shutil.copyfile(path,target)
 h.require(h.sha(target)==record['sha256'],'raw evidence changed')
 sources[relative]=record
h.save(destination/'source-map.json',sources)
h.save(destination/'local-identities.json',local)
(destination/'README.md').write_text(f'''# 统一内存准入限定交付证据

冻结326eff19，生产84d16983、sidecar c5203331、protocol aeb66adc。
{len(sources)}份原记录逐字节复制；{len(local)}个本机ELF/o/bundle只登记身份。
原默认ed672a65/67897c81及完整回退链保留。

真实5生成/4拒绝/2启动和1正常预检首轮通过，业务81.070秒、finalize
106.751秒，单锁COMPLETED/ENV_ABORT0。高floor用真实读数拒绝3个未发送
body的请求；默认门限下普通JSON、strict JSON/SSE、真实项目工具与引用
回填通过。5生成wire+3拒绝wire共17封套；取消/就绪/预检另行校验。

同模型进程压力恢复、实际OOM/cgroup/物理RAM上界没有证明；动态恢复
证据来自原生未加载HTTP/队列注入读数。19原生检查的stdout/stderr仅在
聊天工具记录，未归档原始日志，receipt明确为派生信息；guard、ELF身份
和注册表保留，不为补日志重跑。5个操作间稀疏内存样本不代表整机峰值。
CPU inventory原漂移失败及复核修正保留，其他通过项复用。

详见[结果](../../SERVE_MEMORY_ADMISSION_RESULT_2026-11-03.md)。根seal
校验归档副本，package/execution seal和source-map对应原记录；包含
已退出验收实例的请求凭据，不据此自动重放。未drop_caches或干预他人进程。
''')
h.seal(destination)
files=[p for p in destination.rglob('*') if p.is_file()]
result=dict(raw_records=len(sources),files=len(files),bytes=sum(p.stat().st_size for p in files),local_artifacts=len(local),root_seal=h.identity(destination/'sealed-files.json'))
h.save(base/'archive-result.json',result)
print(json.dumps(result,indent=2))

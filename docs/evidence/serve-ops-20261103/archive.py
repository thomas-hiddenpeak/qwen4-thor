"""Copy frozen operational evidence verbatim; retain large tools/TSDB locally."""
from pathlib import Path
import json
import shutil
import sys

ROOT=next(p for p in Path(__file__).resolve().parents if (p/'AGENTS.md').exists())
sys.path.insert(0,str(ROOT))
from tools.serve_ops import stage
h,base=stage.h,stage.BASE
destination=ROOT/'docs/evidence/serve-ops-20261103'
h.require(not destination.exists(),'preserve original archive')
for folder in (stage.PACKAGE,stage.OUTPUT):h.verify_seal(folder)
summary=h.read(base/'acceptance-summary.json')
h.require(summary['business']['status']=='PASS_BOUNDED_LOCAL_OPERATIONS','acceptance missing')
destination.mkdir()
sources,local={},{}
paths=[p for folder in (stage.PACKAGE,stage.OUTPUT) for p in folder.rglob('*') if p.is_file()]
paths += [p for p in (base/'cpu/owner-correction').rglob('*') if p.is_file()]
paths += [base/name for name in ('archive.py','summarize.py','custody.py','postflight.json',
    'postflight-gpu.json','acceptance-summary.json','owner-rule-correction.py','audit-owner-correction.py')]
for path in sorted(paths):
    relative,record=str(path.relative_to(base)),h.identity(path)
    if '/prometheus/tsdb/' in '/'+relative:
        local[relative]=record
        continue
    target=destination/relative;target.parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(path,target)
    h.require(h.sha(target)==record['sha256'],'raw evidence changed')
    sources[relative]=record
for name in ('prometheus','promtool'):
    local['dependencies/'+name]=h.identity(base/'deps'/name)
for name in ('q4t','q4t.release.json'):
    local['installed/'+name]=h.identity(ROOT/'build'/name)
h.save(destination/'source-map.json',sources)
h.save(destination/'local-identities.json',local)
(destination/'README.md').write_text(f'''# SRV-12 本地运营验收证据

冻结52ada556；运行时198文件及默认84d16983/c5203331保持，protocol aeb66adc。
{len(sources)}份原记录逐字节复制；{len(local)}个本机工具/TSDB/安装pair登记身份。
CPU29+6通过，官方promtool25组/136断言通过，原失败与修复保留。
收尾识别/metrics本身计入普通连接，修复两条owner告警的空闲条件；
旧规则在真实口径的三负控失败，修订8组/97断言通过，其余规则复用。
真实Prometheus加载原冻结规则；此次CPU增量不冒充另一次实际服务运行。

实际3生成/0负例/1模型启动，普通及真实项目资料工具回填通过。
三个只读诊断为HEALTHY/HEALTHY/UNKNOWN，同PID精确计数与隐私哨兵
检查通过。临时Prometheus健康抓取无误报、服务关闭后TargetDown触发；
模型和监控均已停止，GPU锁释放，未安装后台服务或通知接收器。

诊断包原件私有权限见permissions记录；Git副本保留字节，文件权限
不作为原件权限证明。诊断包不含原ID/正文/凭据，但整个验收档案还
包括原wire与服务日志；这些使用临时实例的合成ID/凭据，不称全档脱敏。
未知结果不得自动重放。原TSDB与大工具仍在本机，API观测逐字节入库。

不证明实际日志轮转、生产告警通知、网关鉴权、整机RAM硬限额或长稳。
详见[结果与限制](../../SERVE_OPERATIONS_RESULT_2026-11-03.md)。内层seal
对应原始目录；根seal覆盖副本，source-map登记原路径与字节身份。
''')
h.seal(destination)
files=[p for p in destination.rglob('*') if p.is_file()]
result=dict(raw_records=len(sources),files=len(files),bytes=sum(p.stat().st_size for p in files),
    local_artifacts=len(local),root_seal=h.identity(destination/'sealed-files.json'))
h.save(base/'archive-result.json',result)
print(json.dumps(result,indent=2))

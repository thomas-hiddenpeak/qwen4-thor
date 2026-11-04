from pathlib import Path
import json
import shutil
import sys

ROOT = next(p for p in Path(__file__).resolve().parents if (p/'AGENTS.md').exists())
sys.path.insert(0, str(ROOT))
from tools.serve_pressure import stage
h, base = stage.h, stage.BASE
destination = ROOT/'docs/evidence/serve-pressure-20261103'
h.require(not destination.exists(), 'preserve original archive')
for folder in (stage.PACKAGE, stage.OUTPUT): h.verify_seal(folder)
summary = h.read(base/'acceptance-summary.json')
h.require(summary['business']['status']=='PASS_BOUNDED_PRESSURE_AND_MIXED', 'acceptance missing')
destination.mkdir()
sources, local = {}, {}
paths = [p for folder in (stage.PACKAGE,stage.OUTPUT) for p in folder.rglob('*') if p.is_file()]
paths += [base/name for name in ('archive.py','summarize.py','custody.py','postflight.json',
    'postflight-gpu.json','acceptance-summary.json')]
for path in sorted(paths):
    relative, record = str(path.relative_to(base)), h.identity(path)
    with path.open('rb') as f: magic=f.read(4)
    if magic==b'\x7fELF' or path.suffix in ('.o','.a','.bundle'):
        local[relative]=record
        continue
    target=destination/relative
    target.parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(path,target)
    h.require(h.sha(target)==record['sha256'], 'raw evidence changed')
    sources[relative]=record
h.save(destination/'source-map.json', sources)
h.save(destination/'local-identities.json',local)
(destination/'README.md').write_text(f'''# 同进程压力恢复与混合业务证据

冻结4fd5ec7c，复用实际安装84d16983/c5203331、protocol aeb66adc。
{len(sources)}份原记录逐字节复制；{len(local)}个本机ELF只登记身份。
默认pair未替换，旧回退链保留；源提交/运行身份见package与summary。

326生成尝试（324成功、1主动取消、1排队内存拒绝）、4负例、1模型启动、
65预检首轮通过。同PID真实压力关闭、排队worker回收、运行请求继续、
滞回和恢复，之后64轮普通/strict/真实资料工具闭环；无自动重试。
guard COMPLETED/ENV_ABORT0，独立scope只核算自身后退出，GPU已释放。

continuous.jsonl为每秒非原子计量，各视图不相加。memcg/NVML/RSS并非
全覆盖/独占总RAM；CPU压力是测试客户端，不是专家容量offload。
没有触发物理OOM、没有整机RAM硬限额，也不把本次固定轮数称为长稳。
CPU19+1原始日志完整，运行时198文件未改、零警告/数值证据复用。

详见[结果与限制](../../SERVE_PRESSURE_RESULT_2026-11-03.md)。根seal
核对副本，内层seal对应原文件；source-map保留原始路径和字节身份。
测试凭据属于已经退出的临时实例，不据此自动重放请求或工具。
''')
h.seal(destination)
files=[p for p in destination.rglob('*') if p.is_file()]
result=dict(raw_records=len(sources), files=len(files), bytes=sum(p.stat().st_size for p in files),
    local_artifacts=len(local),root_seal=h.identity(destination/'sealed-files.json'))
h.save(base/'archive-result.json',result)
print(json.dumps(result,indent=2))

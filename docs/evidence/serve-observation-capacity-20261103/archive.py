"""Verbatim SRV-12b evidence archive, with local large/binary identities."""
from pathlib import Path
import json
import shutil
import sys

ROOT = next(p for p in Path(__file__).resolve().parents if (p/'AGENTS.md').exists())
sys.path.insert(0, str(ROOT))
from tools.serve_observation_capacity import stage
h, base = stage.h, stage.BASE
destination = ROOT/'docs/evidence/serve-observation-capacity-20261103'
h.require(not destination.exists(), 'preserve original archive')
for directory in (stage.PACKAGE, stage.OUTPUT):h.verify_seal(directory)
summary = h.read(base/'acceptance-summary.json')
h.require(summary['business']['status']=='PASS_BOUNDED_OBSERVATION_CAPACITY', 'no acceptance')
destination.mkdir()
sources, local = {}, {}
paths = [p for folder in (stage.PACKAGE, stage.OUTPUT) for p in folder.rglob('*') if p.is_file()]
paths += [base/name for name in ('summarize.py','archive.py','custody.py','postflight.json',
    'postflight-gpu.json','acceptance-summary.json','offline-schema.json')]
for path in sorted(paths):
    name = str(path.relative_to(base));record = h.identity(path)
    with path.open('rb') as stream:magic = stream.read(4)
    if magic == b'\x7fELF' or path.suffix in ('.o','.a','.bundle') or '/prometheus/tsdb/' in '/'+name:
        local[name] = record
        continue
    target = destination/name;target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, target)
    h.require(h.sha(target) == record['sha256'], 'copy identity mismatch')
    sources[name] = record
for key in ('promtool','prometheus'):
    local['dependencies/'+key] = h.read(stage.PACKAGE/'dependencies.json')[key]
for key in stage.a.NAMES:local['installed/'+key] = h.identity(ROOT/'build'/key)
h.save(destination/'source-map.json', sources)
h.save(destination/'local-identities.json', local)
(destination/'README.md').write_text(f'''# SRV-12b 观测连接容量验收证据

冻结362f42a8；默认3cd3994d/bb620240、protocol da1466be，199运行时来源。
{len(sources)}份原件逐字节复制；{len(local)}个ELF/object/bundle/TSDB/固定依赖和
安装pair保留本机身份。原package/execution seal不改，根seal校验副本。

CPU OFF226/ON235、必需192、后端9、运营29+6、驱动6、原生配置15
与7成功形状、官方promtool31场景/169断言通过，零警告。一个完整lease
中原生HTTP23项（含已知慢头总额边界）和实际4生成/136负响应/1启动
首轮通过，guard COMPLETED/ENV_ABORT0，没有重试或扩预算。

128普通连接满时四个观测GET、健康、诊断和实际Prometheus抓取可用；
4观测连接满时健康/有效取消可用。131个半体接收连接按EOF/GET体拒绝
释放，普通710003、真实MODEL资料读取/准确引用回填通过。worker2/2、
所有业务owner归零，metrics只占观测1；诊断健康/警告/未知/警告与
故意触发的拒绝一致，不把末尾警告误报成恢复失败。

135原wire加2个HTTP负响应body记录离线schema通过；另3个诊断拒绝只
保存HTTP状态/匿名包和固定counter，不声称保留了这3份原响应正文。
原wire和日志包含临时验收ID/凭据，整个档案不称已脱敏；私有诊断包
在Git中只保留字节，不承诺复制原件0700/0600权限。

临时Prometheus的1秒间隔用于实际解析/饱和抓取，生产15秒规则时序由
CPU场景证明，不冒充生产通知或实测告警延迟。旧84d16983/c5203331
和v3规则77a4ddc6及完整回退链保留，未实际回滚默认或安装常驻监控。
安装CLI/worker通过，HTTP复用同ELF候选，没有额外模型启动。

未知慢头洪泛、实际网关/通知/原始日志轮转、物理RAM硬保护及长稳/通用
恢复仍待证明。详见[结果](../../SERVE_OBSERVATION_CAPACITY_RESULT_2026-11-03.md)。
''')
h.seal(destination)
files = [p for p in destination.rglob('*') if p.is_file()]
result = dict(raw_records=len(sources), files=len(files), bytes=sum(p.stat().st_size for p in files),
    local_artifacts=len(local), root_seal=h.identity(destination/'sealed-files.json'))
h.save(base/'archive-result.json', result)
print(json.dumps(result, indent=2))

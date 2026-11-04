from pathlib import Path
import json
import shutil
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_strict_transport import stage

h, base = stage.h, stage.BASE
destination = ROOT / "docs/evidence/serve-strict-transport-20261103"
h.require(not destination.exists(), "preserve existing archive")
for folder in (stage.PACKAGE, stage.OUTPUT):
    h.verify_seal(folder)
summary = h.read(base / "acceptance-summary.json")
h.require(summary["business"]["status"] == "PASS_BOUNDED_STRICT_COPY_TRANSPORT", "acceptance missing")
destination.mkdir()
sources, local = {}, {}
paths = [path for folder in (stage.PACKAGE, stage.OUTPUT)
         for path in folder.rglob("*") if path.is_file()]
paths += [base / name for name in ("archive.py", "summarize.py", "postflight-gpu.json",
    "postflight.json", "acceptance-summary.json", "memory-summary.json", "analysis-first-failure.json")]
for path in sorted(paths):
    relative = str(path.relative_to(base))
    record = h.identity(path)
    with path.open("rb") as stream:
        magic = stream.read(4)
    if magic == b"\x7fELF" or path.suffix in (".o", ".a", ".bundle"):
        local[relative] = record
        continue
    target = destination / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, target)
    h.require(h.sha(target) == record["sha256"], "raw evidence changed")
    sources[relative] = record
h.save(destination / "source-map.json", sources)
h.save(destination / "local-identities.json", local)
(destination / "README.md").write_text(f"""# 严格复制输出背压限定接受证据

冻结531752ba，专用f07117f3，默认ed672a65/67897c81未变。
{len(sources)}份原记录按字节复制，{len(local)}份大产物仅登记本机身份。
原03b失败/预算及全部默认回退链保留。本包不安装故障程序。

实际6生成尝试/4负例/2启动首轮完成：两个长strict分别244token含EOS，
内容12443字节；真实EAGAIN后的SSE取消/JSON写截止、排队普通/strict恢复
通过。4个完整生成与4个错误封套schema通过，2个故意不完整传输分别保留。
业务{summary['business']['seconds']:.3f}秒、finalize{summary['finalize_seconds']:.3f}秒，
共享GPU锁COMPLETED/ENV_ABORT0，原6/4/2/1800秒预算保持。

这是明确复制条件下固定载荷和缩小窗口的loopback证据，不外推任意
模型输出/代理，也不追认旧模型失败原因。原CPU缺import失败与定向修复、
离线RSS缺字段分析失败均保留，没有为文档或摘要重跑模型。36个稀疏内存
样本，4个退出中/僵尸worker没有VmRSS，按null保留，不补0或相加当物理RAM。
详见[结果](../../SERVE_STRICT_TRANSPORT_RESULT_2026-11-03.md)。

package/execution原seal未改；根sealed-files验证仓库副本，source-map
对应本地原记录。请求凭据属于已退出验收实例，不能用于自动任务重放。
""")
h.seal(destination)
files = [path for path in destination.rglob("*") if path.is_file()]
result = dict(raw_records=len(sources), files=len(files), bytes=sum(p.stat().st_size for p in files),
    local_artifacts=len(local), root_seal=h.identity(destination / "sealed-files.json"))
h.save(base / "archive-result.json", result)
print(json.dumps(result, indent=2))

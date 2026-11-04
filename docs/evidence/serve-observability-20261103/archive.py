from pathlib import Path
import json
import shutil
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_observability import stage

h, base = stage.h, stage.BASE
destination = ROOT / "docs/evidence/serve-observability-20261103"
h.require(not destination.exists(), "archive exists; preserve it")
for folder in (stage.PACKAGE, stage.OUTPUT):
    h.verify_seal(folder)
summary = h.read(base / "acceptance-summary.json")
h.require(summary["business"]["status"] == "PASS_BOUNDED_OBSERVABILITY", "missing acceptance")
destination.mkdir()
sources, local = {}, {}
paths = [p for folder in (stage.PACKAGE, stage.OUTPUT)
         for p in folder.rglob("*") if p.is_file()]
paths += [base / name for name in ("archive.py", "preflight-gpu.json",
                                  "postflight.json", "acceptance-summary.json")]
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
    h.require(h.sha(target) == record["sha256"], "copy identity mismatch")
    sources[relative] = record
h.save(destination / "source-map.json", sources)
h.save(destination / "local-identities.json", local)
readme = f"""# SRV-01b接收拒绝与首次输出失败交付证据

冻结源码/驱动{summary['source_commit'][:8]}，生产{summary['default']['q4t'][:8]} / sidecar {summary['default']['q4t.release.json'][:8]}。
{len(sources)}份原记录按字节复制；{len(local)}份ELF/object/source.bundle仅登记
本机身份；旧c16caf05/be4cd021、原package/execution及完整回退链保留。

真实8生成尝试/16拒绝/2启动首轮完成，业务{summary['business']['seconds']:.3f}秒，
finalize {summary['finalize_seconds']:.3f}秒；一把GPU锁覆盖原生HTTP16、两模型组
与安装健康，guard COMPLETED、ENV_ABORT0。3次预期断连/不完整流不记成功。
生产body容量、读取截止、业务边界、TCP reset与JS真实资料工具；同源专用
普通SSE的EAGAIN取消和30秒写截止、普通/strict恢复通过。严格长payload
背压03b仍未通过，不作性能非劣或工业化整体完成声明。

保留stdout/stderr交错误判及定向修复。日志/固定标签计数对账，已写字节
仅为本地失败逻辑写进度，不是客户端收齐。稀疏parent/worker/global内存
分列，不能当完整峰值。详情见[结果](../../SERVE_OBSERVABILITY_RESULT_2026-11-03.md)。
package与execution原seal不改，根sealed-files验证仓库副本，source-map映射
本机原文件。loopback请求凭据属于已退出的验收实例，不能用作任务重放。
"""
(destination / "README.md").write_text(readme)
h.seal(destination)
files = [p for p in destination.rglob("*") if p.is_file()]
result = dict(raw_records=len(sources), files=len(files), bytes=sum(p.stat().st_size for p in files),
              local_artifacts=len(local), root_seal=h.identity(destination / "sealed-files.json"))
h.save(base / "archive-result.json", result)
print(json.dumps(result, indent=2))

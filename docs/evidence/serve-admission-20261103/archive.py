from pathlib import Path
import json
import shutil
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_admission import stage

h, base = stage.h, stage.BASE
destination = ROOT / "docs/evidence/serve-admission-20261103"
h.require(not destination.exists(), "archive exists; preserve it")
for folder in (stage.PACKAGE, stage.OUTPUT):
    h.verify_seal(folder)
summary = h.read(base / "acceptance-summary.json")
h.require(summary["business"]["status"] == "PASS_BOUNDED_ADMISSION", "missing acceptance")
destination.mkdir()
sources, local = {}, {}
paths = [p for folder in (stage.PACKAGE, stage.OUTPUT)
         for p in folder.rglob("*") if p.is_file()]
paths += [base / name for name in ("archive.py", "prelaunch-environment.json",
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
readme = f"""# SRV-02b准入与容量FIFO交付证据

冻结源码/驱动919cc01f，生产c16caf05 / sidecar be4cd021；专用234fae71不安装。
{len(sources)}份原记录按字节复制；{len(local)}份ELF/object/source.bundle仅登记
本机身份，原文件及旧5642eb68/6aca6a3e回退链必须保留。

真实10生成尝试/4拒绝/2启动首轮通过，81.573秒业务执行、110.008秒
finalize；同一GPU锁覆盖HTTP原生13、两模型组和安装健康，guard COMPLETED。
生产MTP总准入、取消回收、普通恢复和JS真实资料工具闭环；同源专用plain
异容量H0/B1/C0/D0实际次序、队列满JSON/SSE拒绝、头取消与strict恢复通过。
有意取消2次不记成完整assistant轮次；没有自动重试、性能接受或03b背压证明。

10生成wire/17封套与4拒绝响应均保留。配置来源枚举、数组根和数组上限
的CPU夹具失败及定向修复全部保留；未改生产合同迁就夹具。
07:13:45 CST GPU PID空、锁可用、无常驻模型或恢复标记。
稀疏RAM观测和完整边界见[结果](../../SERVE_ADMISSION_RESULT_2026-11-03.md)。

package与execution各自保留原seal；根sealed-files验证本仓库副本。
source-map映射原本机文件，local-identities登记未复制大产物。
编译路径、私有loopback请求凭据仅为本次已退出服务的原始验收记录。
"""
(destination / "README.md").write_text(readme)
h.seal(destination)
files = [p for p in destination.rglob("*") if p.is_file()]
result = dict(raw_records=len(sources), files=len(files), bytes=sum(p.stat().st_size for p in files),
              local_artifacts=len(local), root_seal=h.identity(destination / "sealed-files.json"))
h.save(base / "archive-result.json", result)
print(json.dumps(result, indent=2))

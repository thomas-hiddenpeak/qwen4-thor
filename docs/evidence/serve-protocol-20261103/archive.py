"""Copy exact SRV-08 evidence; keep ELF/source bundles at their original paths."""
from pathlib import Path
import json
import sys

root = Path.cwd()
sys.path.insert(0, str(root))
from tools.serve_protocol import stage

h, base = stage.h, stage.BASE
for directory in (stage.PACKAGE, stage.OUTPUT):
    h.verify_seal(directory)
dest = root / "docs/evidence/serve-protocol-20261103"
dest.mkdir(parents=True, exist_ok=False)
records, local = {}, {}
paths = [p for name in ("cpu", "package", "execution")
         for p in (base/name).rglob("*") if p.is_file()]
paths += [p for p in base.iterdir() if p.is_file() and p.name in
          ("prepare.log", "launch.log", "finalize.log", "postflight.json",
           "cpu-summary.json", "client-release.json", "archive.py")]
for path in sorted(paths):
    relative = str(path.relative_to(base))
    identity = h.identity(path)
    with path.open("rb") as stream:
        elf = stream.read(4) == b"\x7fELF"
    if elf or path.name == "source.bundle":
        local[relative] = identity
        continue
    target = dest / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    h.copy_checked(identity, target)
    records[relative] = identity
h.save(dest / "source-map.json", records)
h.save(dest / "local-identities.json", local)
(dest / "README.md").write_text(f"""# SRV-08合同与JavaScript客户端原始证据

按原字节复制{len(records)}份记录；另有本说明、来源映射、本地大产物身份及根seal。
正式结论见[交付报告](../../SERVE_PROTOCOL_RESULT_2026-11-03.md)。

源码/驱动58c0f2fe；默认5642eb68/6aca6a3e保持不变，189个运行时文件
与SRV-07已接受身份一致。Node24.18.0/openai7.47.0，完整lock及4065文件
SDK树身份在package/sdk.json。client-release.json是独立客户端发布声明，
没有修改旧ELF、sidecar或其原接受范围。

CPU合同396原生结果符合预期；检查器5项整数/字节问题定向修复，391复用。
JS35、桥接/日志12（31参数对照）、最终9项驱动门通过。原缺id加载、
JS语法/package导出、同名模块导入和两次报告格式汇总门失败全部保留；
这些是开发/验收工具失败，没有当作成功删除或覆盖。

真实10生成尝试/16拒绝/1启动完成，含预期的真实TCP答案流中断。9份
完整响应和16错误响应共56封套；435字节中断前缀没有成功receipt。
工具真实读取MODEL、指定不存在文件、同一响应读取MODEL/ARCHITECTURE，
业务错误、完整批次ID回填和引用均验收。显式恢复从已有完整工具结果
重新生成最终答案，使用新凭据，0次额外工具执行；不证明通用任务恢复。

guard COMPLETED、ENV_ABORT0；首次lease到finalize105.73秒，模型进程
正常退出、资源归零。05:51:56 CST GPU PID空、锁可用、无恢复标记/常驻
模型，MemAvailable116.98GiB。本批不是性能优化或工业化总体验收。

{len(local)}个探针ELF/source.bundle仅登记本机原路径、大小和SHA；完整
内部包seal需要这些本地文件。所有wire、工具结果、日志、观测、guard、
CPU与失败记录保持原字节。不要删除依赖这些原路径的工作区或回退证据。
""")
h.seal(dest)
for relative, identity in records.items():
    assert h.sha(dest/relative) == identity["sha256"]
files = [p for p in dest.rglob("*") if p.is_file()]
print(json.dumps(dict(copied=len(records), local_artifacts=len(local),
    files=len(files), bytes=sum(p.stat().st_size for p in files))))

# 本轮第二级无损压缩救济（仅准备）

依据 `source/docs/EVIDENCE_STORAGE.md` 最后一节“新生成 NormGate
证据无损压缩”：仅处理本轮新生成、nlink=1、最后消费已完成文件。
旧 manifest/来源 ledger 不变。原路径需按 durable compression map
还原；gzip 不可直接代替 SQLite/HTML/JSONL 文件使用。

目前仅做静态依赖检查与前 15 个终态服务的元数据枚举：235 个候选，
实际分配 50,626,560 字节（48.28 MiB）。其中前 16 组允许 SQLite
`benchmark_data.db` 和 `perf_report.html`；仅 m052/m061 额外允许
生成的 `http/inputs/*.jsonl` 与各 case/run `requests.jsonl`。
未读取候选正文，未查看当前 m061 内容，未压缩或移除文件。

最终 m061 清单只能在其 stage、HTTP audit 均通过、进程与 unit 全部
结束以后实例化。所有候选上限 260 个、单文件 16 MiB、原始总量
100 MiB、哈希/压缩读取/解压复验 512 MiB。实际节省未知，不保证门
一定恢复，不能为达到门扩大范围或降低既定门槛。

静态依赖结论：SQLite 由 run_acceptance 在每次客户端终态时读取并
生成 responses/request-boundaries；HTML 为生成报告；请求 JSONL
为生成副本。后续 audit_stage/resource_audit/compare/independent
不读取这些原路径。资源审计仍需原 logs、resource streams、endpoints、
boundaries/client-exit 与 copied tools；这些全部排除。每个现在的
source_sha256/frozen_files/metadata_source_sha256 映射还会在 prepare
与 execute 中递归检查，命中任何候选即停止。冻结 fixture 不在范围。

所有已有首失败与静审发现保留。`storage-compression-policy.json`
绑定依赖源码 SHA、固定范围及已观察的 15 组具体路径。最终清单由
prepare 命令生成，root 再冻结其 SHA，execute 才能处理。

执行前置条件（脚本也检查）：

1. 原硬链接脚本已成功且 ledger/原字节保护成立。
2. 当前实际 free 仍低于原 1,073,741,824 字节门。
3. 前 16 个 group stage/controller/audit/decision 均通过且身份正确，
   无 owned active unit，m062 尚未启动。
4. 完成独立静审；两个阶段各只运行一次，失败禁止自动重试。

服务间隙，root 先运行：

```sh
python3 -B "$R/storage_compress.py" prepare \
  --policy-sha256 68861179bfe11132dc4c823c1bd210cd47520b8a508282976a5fe25e9890aa50
```

检查并冻结 `R/storage-compression-plan.json` 以及脚本 SHA；保留
prepare 输出的 `manifest_sha256`，再执行（HASH 替换成真实摘要）：

```sh
python3 -B "$R/storage_compress.py" execute \
  --policy-sha256 68861179bfe11132dc4c823c1bd210cd47520b8a508282976a5fe25e9890aa50 \
  --manifest-sha256 HASH
```

execute 首先预检全部最终候选原 SHA/长度/元数据。之后按分配大小
降序处理，逐文件 gzip level1，fsync 完整压缩文件，流式解压核对
原 SHA 与长度；将原路径、SHA、长度、原元数据与压缩文件路径/SHA
写入 `storage-compression/compression.json` 并 fsync/原子替换持久化，
之后才移除对应新原件、fsync 原目录。压缩未减少实际分配时保留原件。
一旦原门已达到，即停止，剩余候选保持原状。

压缩结果只进入新 `R/storage-compression`。每步 journal fsync；异常
保留 `storage-compression-first-failure.json`、所有 gzip、map、journal。
若中断发生在 durable map 写入后而原件移除状态还没更新，可依 map
完整还原，不能据状态标记盲目重试。未承诺 inode/mtime/mode 不变。

完成后只读原 compact ledger 摘要确认未改，不重读旧 raw/model，
不运行新模型测试或改源码。最终交付归档应加入 policy/final plan、
独审、script、map、summary、所有失败记录，并把 gzip 列为本地 raw
及 SHA；不把压缩过程或空间回收视作性能、正确性或物理 RAM 验收。

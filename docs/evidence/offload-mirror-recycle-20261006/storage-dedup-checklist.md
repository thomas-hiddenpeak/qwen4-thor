# 本轮服务间隙存储去重清单

本文件与 `storage_dedup.py`、`storage-dedup-plan.json` 仅已准备；未执行
候选内容哈希、去重、删除或模型测试。不得在当前 m061 运行期间执行。

固定范围为 execution-plan 中 q01 至 m051 的前 14 个已终态服务。
逐文件路径、原 device/inode/mode/mtime/nlink/逻辑与分配大小及已有
SHA 绑定，全部列在 `storage-dedup-plan.json`。没有重新读取候选 payload。

| 类别 | 数量 | 分配字节 | 可释放字节上界 |
|---|---:|---:|---:|
| 生成 input/request JSONL 副本 | 323 | 37,662,720 | 30,896,128 |
| 本轮各服务 tools 源码副本 | 130 | 2,662,400 | 2,457,600 |
| 可重建 .o/.o.d/.a | 200 | 15,028,224 | 15,028,224 |
| configure probe 二进制 | 8 | 2,195,456 | 2,195,456 |

硬链接上界按同长度/最终只读 mode 分桶计算，每桶保留至少一份。
尚未读内容，不能据此认定同长度即相同内容。硬链接最多 31.81 MiB；
加可重建文件最多 48.23 MiB。真实释放以完整字节比较和 journal 为准。

8 个 probe 仅为 CMakeFiles/4.4.3 内两份 CMakeDetermineCompilerABI
`.bin`、两个 CompilerId 目录的 `a.out`、CUDA tmp 内两份 `.fatbin`
及两份 `.cubin`。保留 `.cmake`、编译器源代码、预处理/生成代码、
配置日志、编译 flags/link 命令、运行与测试可执行文件、source/export
及 candidate-source.tar。后者受 build-identity.source_sha256 间接绑定。

执行前由 root 确认：

1. m061 已有终态 stage/controller-exit，cleanup 完成；m062 尚未启动。
   脚本也检查所有已启动服务的终态记录及 systemd unit MainPID=0。
2. 独立静审脚本与 plan；记录这两个新文件自身 SHA。冻结的实验
   plan/source/门槛/工具均不改，不重验已通过 HTTP 或数值测试。
3. 只执行一次，输出目录 `R/storage-dedup` 必须不存在。失败保留
   operation/preflight/journal/failure，不自动重试，也不清理失败证据。

服务间隙命令（`R` 指本轮实验根目录）：

```sh
python3 -B "$R/storage_dedup.py" \
  --scope-sha256 3feb568e7d8884ae08dde4050edf47220a11b3a93852549a47e21ee705658263 \
  --execute-after-m061 \
  --prune-rebuildable-if-needed
```

首先完整预检所有候选原路径及字节 SHA，并与现有 stage source ledger
的相关 SHA 一致，再逐字节比较拟共享文件。只有完全相同的字节允许
共享；使用临时硬链接与 os.replace 保持原路径始终可读。共享文件改为
只读，每个操作先 fsync journal。事后逐路径验证字节，并复核本轮
原始 ledger 文件 SHA 不变；不递归重读旧原始证据。

硬链接完成后实际 free 已达到原定 1,073,741,824 字节时，保留全部
可重建文件。仍不足才按清单选择能单独补足的最小文件；不存在时选
最大的剩余文件。每次删除前记录该文件原 SHA 和元数据，记录实际
free 前后变化，足够即停。脚本不会改实验启动门，实际下一阶段仍由
原 frozen runner 判断。报告的 allocated bytes 与观察到的 free 差值
分别记录，不能当作推理内存改善。

不得触碰 m052/m061/m062 的证据内容；运行期间停止任何此类存储 I/O。
不涉及 SQLite、HTML、server log、responses、资源流、旧树、模型、
reference、cache 或 frozen 输入。原始内容与来源 ledger 保持；共享的
inode/nlink/mtime/readonly mode 可能变化，原值保存在 preflight/journal。
未来若要写某个共享副本，先新建独立副本并核对字节，不得就地修改。

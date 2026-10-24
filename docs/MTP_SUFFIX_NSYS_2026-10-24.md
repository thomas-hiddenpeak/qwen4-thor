# 固定 k3 suffix：Nsight 收尾完整性有界诊断

2026-10-24，用户新授权。先修复/验证采集工具，再补缺失 GPU 对照；这是工具
缺陷与观测阶段，不是新的运行时优化验收。原阶段及旧 warning 不追认通过。

## 开测前冻结

- 仅两个机制：非 CUDA wrapper 的进程角色；应用 range-stop 与外部 session-stop
  的最终化差异。不设置隐藏 CUPTI 参数，不增加模型同步，不换工具/驱动。
- 同一已知工作量 fixture 四跑，顺序 direct-stop、wrapper-stop、direct-external、
  wrapper-external。固定 32 kernel、32 H2D、32 D2H、33 闭合 NVTX range；独立
  CPU ledger 和 GPU 结果逐项校验。四跑都完整执行，不用诊断 warning 代替数量合同。
- 每份 SQLite 独立副本各删一整对 API/kernel、末 NVTX range，两种负控必须拒绝；
  不改原始证据。正例的 warning 只允许说明该 fixture 是否仍完整，不推广任意输入。
- 公共公开选项 trace=cuda,nvtx，sample/cpuctxsw=none，process-tree，
  capture-range=cudaProfilerApi，flush-on-cudaprofilerstop=true，kill=none，wait=all。
  stop/none 仅按冻结矩阵变化。external 在应用 Stop 成功 receipt 后执行唯一命名
  session 的 nsys stop，随后正常释放目标；命令成功不等于 trace 完整。
- fixture 开独立 NVLOG global100/Cupti_Public100，保留 PID/TID/flush/terminal
  原始记录，日志合计上限 256 MiB。模型不增加全量 NVLOG，保持原 CPU/NVTX 观测。
- 唯一可进入模型的候选为 wrapper-external；known-work、负控、正常退出和
  实际目标 diagnostics 必须通过。实际目标仍有不完整 warning、显式丢失、未知
  错误或无法闭合的终态则停止，不扫描第三种采集策略。非 CUDA wrapper 的空事件
  warning 只能在精确 PID 角色及零 CUDA/NVTX 记录下单列，不能豁免实际目标。

CPU 合同上限 30 methods、两次 suite、总 180 秒；第二次仅限具体失败修复。
必要 fixture 构建不算测试，零警告；不重建 q4t。GPU 阶段全程同一共享独占锁，
四 fixture/1200 秒，随后最多 6 HTTP/4 模型启动/3600 秒，收尾 600 秒，
总墙钟 5400 秒。单 fixture 300 秒、模型组 900 秒，全部裁剪到总预算。
不重试、不预热、不冷缓存、不改时钟/亲和性。环境竞争或监控失败 ENV_ABORT，
只停止自有进程；1 Hz 轮询不保证不存在更短干扰。

GPU 模型顺序固定子集 8K OFF、8K ON、200K ON、200K OFF；两个 200K 组各保留
一个不采集的 8K 前驱，合计六响应。输出 256，greedy/k3，材料、输出 SHA、容量、
缓存条件继承前阶段。使用 f0ff0fe8 观测程序（源码 55363f18），默认交付 f356b2c9、
sidecar 和 b63b0f25 子集 manifest 不变。完整身份及每个可执行依赖写入新 plan。

## 明确复用与归因边界

直接复用上一阶段 37 正常 HTTP、6 CPU 观测、39 监控 CPU 合同及身份未变的
数学/质量合同，范围以原文为限。两种 head 重复收益：完整代码/文本
13.68%/14.40%，子集共享 B 描述性 GM 6.53%/6.73%；不重跑、不合并成独立启动。
这不是优化五档保护通过或总体统计非劣。详见
[上一阶段](MTP_SUFFIX_EXECUTION_RERUN_2026-10-24.md)。

新分析分别列 CPU 墙钟、线程 CPU、API 区间并集、GPU 执行并集及同 Nsight
时钟的空隙；按 prefill chunk、prompt extend、proposal、accepted extend 分组。
CPU/NVTX 只按身份键对应；不强行补齐空的时钟 offset 交集。线程 CPU 包含
驱动提交和忙等，墙钟减 CPU 不能当 GPU 时间；profile 时间不当无插桩吞吐。
成功应用 launch 必须与活动反向核对；仅有完整 CUPTI flush containment 的工具
线程活动可分离，未知缺口停止。没有具体因果证据则不提出运行时 B。

结束分别记录证据有效性、测量完整性、统计结论和工程决定；预算用完不补样。
工具可审查修复按阶段提交推送；没有新的完整优化验收则保留默认程序。

## 首次测试前的具体观测修订

静态只读 SQL 复查旧 SQLite，14283 次成功 `cudaMemsetAsync` 中 7902 次有
MEMSET 活动，其余 6381 次没有任何 kernel/copy/memset；其中 6216 次恰为
74 次 verify 各 84 次。API 表无调用字节数，不能据此断言丢事件或合法无 GPU
路径。此查询前后原 SQLite SHA 不变，独立结果 `memory-coverage-old.json` 保留。

在 **0 次测试/0 次采集** 时修订同一 fixture：每轮 H2D 后、kernel 前增加一次
非零 memset，偶数轮 512 字节、奇数轮 16384 字节。仍是 32 轮、32 kernel、
64 copy、33 NVTX，新增 32 memset API；逐元素输出验证其实际效果。已有初版
零警告构建及源文件独立保留，重新必要构建。没有新增采集或墙钟预算。

MEMSET 活动按调用、字节数、顺序单独核对，缺少活动记录为
UNKNOWN_CAPABILITY，不假称实证丢失。四组既定对照可完成 known kernel/copy/
NVTX、API/输出和负控检查；唯一候选还必须闭合 GPU memory coverage 才进入
模型。这样能在小用例中暴露已知表示能力问题，不耗用模型请求重复发现它。

## 结果

30 项 CPU 合同两次 suite 均通过（合计 1.679 秒，0 失败/跳过）。首次通过后
静态审阅发现独立分析 API 未拒绝 UnknownLevel，以及 pidfd 把错误 poll mask 当成
退出的具体漏洞；修复并补原方法内反例后执行唯一第二次 suite，预算已用完。
首次/最终源码、日志及恢复 SHA 凭证均保留。固定非零 memset fixture 零警告构建。
以下记录随后唯一一次 GPU 阶段。静态研读、原 trace 法证和官方版本资料保存在
`.q4t-work/suffix-nsys-20261024/`；原始含环境信息的 profile 元数据不入 Git。

### 有界执行终态：ENV_ABORT

冻结 plan `a995b1e6526d`，1394 文件；实际完整持锁驱动 **5.229048 秒**，只启动
direct-stop fixture，**0 模型加载/0 HTTP**。fixture 完成输出及退出、SQLite
导出返回 0，但监控 6 次尝试仅写出 5 次，第六次失败；原 stage/lease/raw 均保留。
未完成在线 fixture group、真实采集副本负控、其余三种控制和四组模型对照；
不存在 candidate gate，通过的 synthetic CPU 负控不替代这部分未测量。

失败对象是 fixture PID3189205/start99613070，不是 evalscope 客户端。ready 后
16.986ms 读取 `io` 遇 EACCES；同身份初读/复查均 R，pidfd 已绑定活身份，
实际等待 49.064ms 后仍未 ready，判为 LIVE_TASK_EXIT_TIMEOUT。名义 50ms 窗口
剩约 0.865ms，不能声称完整观察了恰好 50ms。原始 stat vsize/RSS 为 0、
status FDSize=0 且无 Vm 字段；结合后续正常 main-return/退出观察，强烈指向
退出清理窗口，但不证明内核权限原因，也不把 R 解释成应用仍在推理。
后续退出 receipt 无单调时间，不能还原完整拒绝到退出时长。没有扩大等待或重试。

### 已知工作完整与警告不是同一个结论

环境终止后，仅对已封存 SQLite/日志进行一次只读法证，不重新导出或采集。
独立审阅闭合全部 32 kernel、64 copy、32 memset、33 NVTX 和 32 个输出，
512/16384 字节 memset 各 16/16 有对应活动。实际目标仍有两条警告：
`Not all NVTX events might have been collected.` 和
`Not all CUDA events might have been collected.`

因此这份固定工作的已知事件没有缺失；警告不能直接等同于这些事件丢失。
没有 wrapper 也出现同样警告，排除“仅包装进程空事件”作为其充分解释。
NVLOG 记录应用 Stop 的普通 flush flag0，随后控制线程 flag1（CUPTI FORCED），
再发 CUDAFinish/ReportCUDADone DONE 与 NVTXFinish；退出时还有后续 forced flush。
这是 producer 顺序及局部合同证据，不证明 consumer 已接收所有终态，也未定位
私有 TraceSummaryVerifier 的触发分支，**不能统一宣布旧模型警告为误报**。
producer304/importer292 的计数域不同，不能称丢了 12 条；未出现明确 drop
计数不能填成 0。NVLOG 中 context-disable/HES 不支持信息保留，未猜测归因。

旧模型的 6381 次 memset 缺口仍未知。每轮 verify 的 84 次与源码中的 36 个
linear layer 各两次 N=48 投影、12 个 full layer 各一次 N=128 投影数量吻合；
这些小 N 回退 cuBLASLt，调用点无显式 memset，可能涉及库内部调用。但没有
API 字节数或内部参数证据，不能证明零长度、合法无 GPU 活动或真实事件丢失。
新 fixture 的非零 memset 成功覆盖不能推广到这些库内部调用。

### 四字段与收尾

| 字段 | 结论 |
|---|---|
| 证据有效性 | 身份/封存完整性 PASS；环境 ENV_ABORT。局部 fixture 法证不恢复阶段 VALID |
| 测量完整性 | 未完成四控制及 GPU 模型矩阵；200K/8K 无新 GPU 差分 |
| 统计结论 | 无新推理性能样本，无法更新退化或修复结论；重复收益只复用原范围 |
| 工程决定 | NOT_ACCEPTED_TOOL_VALIDATION_ABORT；保存研究工具及证据，不部署、不扩样 |

独立审计一次完成：1394 冻结/22 raw/453 源快照/197 模型 metadata 身份一致，
6 个已知自有进程身份已消失，历史进程组无残留。失败 fresh-monitor barrier
明确 complete=false，成功 barrier 为空；没有把清理完成伪装成监控完整。
默认 f356、发布 sidecar、显式子集与 f0ff 观测程序均保持。完整 head 重复
代码/文本 +13.68%/+14.40%、子集共享 B GM +6.53%/+6.73% 沿用上一有效阶段，
不追加样本、不以此抵消未完成的五档保护或 GPU 归因。

[精简证据](evidence/mtp-suffix-nsys-20261024/summary.json)、
[已知工作法证](evidence/mtp-suffix-nsys-20261024/result-analysis.md)、
[收尾机制法证](evidence/mtp-suffix-nsys-20261024/collector-result-review.md)、
[库调用候选关联](evidence/mtp-suffix-nsys-20261024/memory-source-review.md)。
独立审计 SHA `92f57626fce8`，原始数据位于新阶段目录，仅按失败现场归档。

591 条目、41361125 字节独立拷贝并 SHA 回读通过，manifest `b971c03b0d7f`、
seal `685486c0ab4d`。归档包括两次 CPU 源码/结果、采集失败现场、法证脚本与
原报告、候选工具及 f0ff/6a/f356 程序、sidecar/子集 manifest；原路径保留。
上一有效局部证据的独立归档 `c97deae2c1bd` 继续保留引用。公开 MD 仅修正
证据链接和“库内部”未证措辞，原报告不改。封存后的本段与 summary 归档索引
由阶段 Git 提交保存，不伪称已在自身 seal 内。没有发布替换或新增健康请求，
保留 codex/suffix-k3 的独有研究提交，不清理证据路径或自动重开测量。

# Decode 调度与回退路径一致性定位

状态：候选未接受、未提交；更新 2026-09-22。父版 7364767。
代码和原始 HTTP 记录是事实来源，本报告不替代精度或性能验收。
所有证据目录位于 `.q4t-work/e2e/`，实验都使用 tools/evalscope
真实 HTTP，必要构建后没有前置单测、数值测试或 profile。

## 当前结论（2026-09-22）

以下章节的“正在进行/尚未运行”描述保留定位当时的状态；本节和
STATUS.md 是当前进度入口，不能把早期候选结果套到最终二进制。

- 原 11 题质量 HTTP 已通过，答案、完整输出和输入摘要保持
  7364767。正式五档输出门禁仍失败：修复后的调度输出不同于
  原调度参考。此失败没有被独立性能定位覆盖，也没有重置参考。
- 旧回退→候选调度→旧回退五档 45 请求已完成，各版输出保持
  旧回退。临时日志清理、池基址寻址修正后重新经过质量与五档
  HTTP；prefill TTFT 回退得到修正，短档 decode 差距仍存在。
- 短 indexer 的四 BF16 向量读取因 8K TTFT 双侧不利分离已撤回。
  保留标量点积顺序，只改变 block 从 8×256 到 64×32 的候选
  五档完成，未发现相对标量版不利范围分离，仍非正式接受。
- 当前 GDN packed 状态 float4 加载/存储候选，质量 11/11、五档
  15/15 原始 HTTP 长度/停止原因/候选输出检查通过、服务退出 0。
  完整输出保持前一候选，正式父版输出差异仍在。它只改
  PackedDecode 且 ROWS=4/8 的状态访问，不改变递推计算体。

当前二进制 SHA-256：
`2e8d275667c85ceb0cd188a7f602878ee76da4409b74fb9f5984b72a363e4bf6`。
证据目录 `gdn-packed-state-vector-performance-20260922`，
`raw-response-review.json` 逐项核对原始响应和多个独立参考。

| 输入 token | TTFT 均值秒 | decode tok/s | 与正式父版原范围比较 |
|---:|---:|---:|---|
| 1024 | 0.798285 | 18.740671 | 两指标重叠 |
| 4096 | 2.638415 | 18.029645 | 两指标重叠 |
| 8192 | 5.180500 | 18.040102 | TTFT 不利分离，decode 重叠 |
| 45056 | 30.536025 | 17.843248 | TTFT 不利分离，decode 重叠 |
| 204800 | 159.466187 | 17.006620 | 两指标重叠 |

每档三次、输出 256；MTP off、max_seq=1、max_prefill=8192、
max_len=208896。TTFT 是客户端指标，不是纯 prefill 耗时。
与紧邻线程块候选相比，五档 decode 范围均提高；该结果不能
抵消正式父版 8K/44K TTFT 风险，也不能证明全面精度保持。

`gdn-packed-state-vector-parent-pair-20260922` 已完成
8K/44K 父版→候选→父版，各三次。两版各自对照原完整输出；
跨版本输出不同，不能将耗时差全部归因于某个 kernel。

三侧十八请求全部完成，驱动及三个服务退出 0，各版输出保持
自身参考。8K 两指标与双侧范围重叠；44K TTFT 与双侧重叠，
初次 TTFT 不利分离未复现。但 44K decode 候选最大值
17.856872638 tok/s，后侧父版最小值 17.857021871，形成单侧
不利分离（与前侧重叠）。差距小不能自动忽略，整体仍不接受。
原始审查明确标记该项；额外“全部范围通过”的汇总断言因此失败，
不影响十八个 HTTP 已完成的事实。已启动独立 44K 三侧复核
gdn-packed-state-vector-44k-confirm-20260922，PID 1827688；
源码/二进制不变，不替换之前样本，不设置新容忍百分比。

尚未闭合：44K decode 单侧差异复核、正式输出差异的精度处置、
真实 B>1、
非零/复用 slot、mixed rope_delta、跨短长 indexer 边界批处理、
视觉/视频与 MTP 消费者。现有消费者工具要求的正式完整门禁
尚未满足，不伪造 acceptance.json，也未开展低层数值/profile。

## 问题与范围

正常 SchedulerLoop 即使 B=1 也调用 ModelDecodeBatchMulti；
调度 logits 分配失败时的回退调用 ModelDecodeStepSeq。
使用已接受二进制，1K、8K 文本和显式模板单图分别三次，按
调度→回退→调度重启对照：路径各自稳定，三类输出均不相同。
故该问题不是本次候选才引入。不同路径输出相同也不证明模型
精度正确，需要另外通过固定质量和参考精度审查。

## 两个独立边界

1. RoPE：单序列 decode 写入当前 logical position + rope_delta
   的三行坐标；旧 ModelDecodeBatchMulti 没有对应写入，实际
   forward 却使用 pooled rope 表。最小候选按实际 seq_id 补写。
   该候选改变调度输出，保持回退输出，但未消除三类路径差异。
2. GDN：在补写坐标后的实际单图首步 decode 内，日志逐层缩小
   差异，最终定位在第 0 层 Gated DeltaNet recurrence。
   初始 SSM/conv 状态、四项投影和卷积输出摘要相同，recurrence
   输出与更新后的 FP32 状态首次不同。

摘要一致仅用于定位，并非完整张量逐位精度检验。插桩显式开启，
不用于性能结论。每轮都核对生成输出未被插桩改变。

## 证据链

| 目录（均有 -20260921 后缀） | 实验 | 已确认结果 |
|---|---|---|
| decode-rope-diagnostic | 父版三类请求、三侧共 27 请求 | 重启一致，三类调度与回退均不同 |
| decode-rope-fix | 最小 RoPE 补写、同组 27 请求 | 回退不变，调度改变，仍与回退不同 |
| decode-rope-trace | HTTP 内实际 logits/top1 日志 | GPU 选择等于 host top1；首步已不同，token 分叉晚于分布分叉 |
| decode-rope-shared-gdn | 两侧 Q4T_GDN_REG=0，单图 9 请求 | 仍不同，单靠去除 Q/K BF16 中间舍入不能解释全部差异 |
| decode-rope-layer-trace | prefill/首步逐层摘要，9 请求 | prefill 48 层摘要一致，首步输入一致，第 0 层输出开始不同 |
| decode-rope-stage-trace | 层内阶段摘要，9 请求 | PLE 后及 attention 输入相同，attention 输出首次不同 |
| decode-rope-linear-trace | 状态/投影/conv/SSM 摘要，9 请求 | 状态输入与 conv 输出相同，SSM 输出首次不同 |
| decode-rope-gdn-shared-order | 共享模式下统一归约与更新函数，9 请求 | 三侧输出及全部边界摘要一致 |
| decode-rope-gdn-register-multi | 默认模式复用寄存器计算体，9 请求 | 三侧输出与边界摘要一致，保持原默认回退输出 |

各已结束的三侧实验服务退出均为 0。后几轮的 complete-review.json
记录完整审查；partial-review.json 保留当时未完成重启的中间观察。

## 当前候选

除 RoPE 补写外，GatedDeltaNetRegKernel 新增编译期 packed decode
模式：grid.z 选择一个 packed token，d_seq_id 选择真实池状态，
每序列只推进一次。Q/K 使用与单序列相同的预归一化，复用相同
计算体，单序列保持原 token 循环。共享路径复用同一 norm 归约、
GdnKSum 和 GdnUpdateY。MTP 多 token causal 分支尚未改变。

这不是把 B=1 绕回单序列，也不是将 Q4T_GDN_REG=0 作为生产设置。
当前单图对照仅覆盖 B=1，不能证明实际 B>1 状态隔离或非零 slot。
默认寄存器与共享模式分别有不同 Q/K 舍入与累加顺序，不能以数学
公式相同或 near-tie 一词替代精度审查。

## 未完成的门禁

- 同二进制 1K/8K/单图三侧共 27 请求正在进行，目录
  decode-rope-gdn-register-text-20260921；仅保留 decode 日志。
- 首组调度三类均已完成，1K/单图匹配旧回退，但 8K 不匹配旧
  回退，记录 prior-fallback-partial.json。新回退及重启尚未结束，
  该变化必须保留，不能仅以新版本路径相等判定精度保持。
- 严格质量 11 题已安排依赖执行：必须先确认上述进程结束、三类
  路径及重启输出一致、二进制摘要不变。目录
  decode-rope-gdn-register-quality-20260921；清除日志/实验环境，
  对照 7364767 原质量输出，不更新参考。
- 五档输出 256 的完整矩阵、直接父版与固定性能参考对照未完成。
  旧调度路径输出可能变化，必须显式保存和解释，不能悄悄替换基线。
- 实际多序列、池 slot 复用、生命周期、视觉/视频、MTP HTTP 未完成。
- 仅在完整所需 HTTP 门禁通过后，才能开展参考精度及性能细分析。
- 临时定位接口与日志需在最终候选中清理或明确保留；任何运行时
  清理后重新构建，首项仍为真实 HTTP，再验收最终二进制。

在这些证据齐全前，不宣称生产接受、精度无回退或吞吐改善。

## 8K 扩展中的进一步定位

当前新回退 1K/8K 均保持旧默认回退输出；新调度仅 8K 不同，
所以不是将所有变化都归因于单序列寄存器计算体改动。
三侧 27 请求的重启组尚未收尾。质量依赖条件因 8K 路径不一致
预计拒绝启动，不能视为质量题已经执行或失败。

已安排同二进制 8K 三侧首步/完整 prefill 边界日志，目录
`decode-rope-gdn-register-8k-trace-20260921`，等待上述真实进程
及依赖驱动结束后才启动，不并发占用模型服务。

静态线索：FullAttentionForward 在 n_groups_max <= max_blocks
时，d_seq_id==nullptr 使用 Bf16Gemm + IndexerReduce，而调度
T=1 使用 IndexerDecodeScores；超过该边界的小 T 则共用
OnePassScoreKernel。这只是待验证假设，需逐层/阶段 HTTP
日志证明差异实际发生的位置，不能直接称作根因或量化噪声。

## Indexer 舍入的受控结果

full attention 内部日志九请求结束：第 3 层 query、gate、index
query、有效压缩 key 摘要相同，评分和选中位置序列不同；插桩
不改变输出，各路径重复/重启稳定。单凭位置摘要不推断集合差异。
证据 `decode-rope-full-attention-trace-20260921/complete-review.json`。

专用 T=1 评分仅增加每 head 点积的 BF16 舍入，原缩放和选择
过程保持。`decode-rope-indexer-dot-round-20260921` 的调度、
回退共六请求已完成，均匹配旧默认回退输出；调度的首步完整
主干/子阶段/linear 摘要亦与旧回退一致。评分摘要仍不同，不能
宣称全部数值逐位一致。重启组仍在运行。

严格质量驱动 `decode-rope-indexer-round-quality-20260921`
已安排：只在前一真实进程结束、全部路径和重启比较通过、
二进制摘要不变后，清除所有 Q4T_ 与 LD_PRELOAD 环境运行
原 11 题 HTTP，对照 7364767 原输出。不会改写质量基线。
此时质量题尚未运行；首轮 8K 修复前的未启动记录保持原状。

该舍入改动只用于验证已定位的 T=1 分支原因。尚未统一实际
多 token indexer 的精度边界，不作为最终多序列实现或生产
精度结论。所有原始分叉、失败和中间观察均保留。

## 2026-09-22 质量与严格性能门禁

点积舍入三侧九请求已完成且输出一致，服务退出 0。
无定位环境的原 11 道质量题全部精确通过，prompt/完整输出
与 7364767 相同，长度/停止原因正确；quality-review.json 留证。

严格性能门禁在 1K 首档退出：相同 prompt、三次稳定、各输出
256，但文本与旧调度参考不同，后四档未运行。失败位于
`decode-rope-indexer-round-performance-20260922/failure-review.json`。
这不作为性能回退数值结论，也不能因质量题通过而接受。

当前独立定位矩阵 `decode-path-parent-fallback-matrix-20260922`
比较旧版回退→候选调度→旧版回退，覆盖五档各三次、输出 256。
父版 binary 与已接受 7364767 摘要相同；候选与后侧父版均严格
对照前侧回退输出。不修改或替换旧调度参考，结果即使通过也
不自动接受；它用于确认修复是否保持已有回退路径的完整行为。
在所需 E2E 尚未闭合前，不进入底层数值或性能细分析。

# 当前状态

更新：2026-09-21。代码是实现事实来源；验收规则见 [EVALUATION.md](EVALUATION.md)。

## 目标与约束

先完善 runner，以保持当前精度、各档性能不回退为门禁，逐步演进模型
专用数据流引擎。性能持平且净复杂度下降可以接受，但不能把类型封装、
计划文档或生命周期缩短直接当成已测吞吐/峰值容量收益。

用户已授权自主推进、修正、回退和阶段 commit/push main，无需常规请示。
模型目录与 reference/ 只读，保护其他任务的未提交内容。运行时改动
必要构建后，第一项测试必须是 tools/evalscope 真实 HTTP；质量与完整
五档性能全部通过后才做数值、单测或 profile，不使用旧 bench。

固定基线：MTP 关闭、单流、greedy；输入 1024/4096/8192/45056/204800，
每档同输入三次、输出 256，max_prefill=8192、max_len=208896。
TTFT 包含 HTTP/分词/prefill；decode 按首 token 后总生成数/总时间。
按重复范围比较，不设置临时容忍百分比或拼接最优档位。

## 最新接受：单序列 prefill 仅计算末行输出头

基于 46b732b，新增显式 LogitsRows：默认全部行，末行模式仅
对最后 trunk 行执行 mixer/词表投影，输出写 logits 首行，
trunk_out 仍保留全行。普通文本调度中的单请求短 prefill 与长
prefill 末块接入，批量 B>1 仍保留各序列末行的全行路径；MTP、
视觉及内联回退默认语义不变，logits 预留容量尚未缩减。
中间接口未改齐时的编译失败保存在 build-incomplete-edit.log；
最终重构建零警告，首项质量 HTTP 11/11、输出对照通过、服务
退出 0，完整五档 HTTP/输出 15/15、双参考范围检查通过。
4K/8K TTFT 均值改善约 1.6%–1.7%，44K/200K 不认定稳定收益；
并发相邻对照 18 请求通过，三个服务退出 0，总完成时间与两侧
父版范围均重叠；四项生命周期 HTTP 已通过，没有前置测试。
证据 prefill-last-row-20260921；完整质量/性能和调度 HTTP
全部通过后才进行数值与时间线，详见
[P7 验收报告](../dataflow-engine/PREFILL_HEAD_DEMAND.md)。
本阶段经独立精度与时间线核对接受，固定参考不更新。
门禁后数值工具零警告运行完成：220416000 个 trunk BF16 值、
7 组固定 token 续算逐位一致，canary/序列状态通过；10 组 logits
中 8 组非逐位一致，最大 l2_rel=0.000919、max_abs=0.0625，
10/10 argmax 与 top2 值相同。近零值也存在符号翻转，不能统称
“一 ULP”。逐阶段确认 normed 一致、mixed 四组差 1–9 元素；
同 BF16 权重的 CPU FP64 参考显示末行 mixed 全部匹配，最终
logits 对参考误差八组更小、两组相同、argmax 全对，按 E1 完成
精度审查（不是 E0）。4K/44K 时间线确认仅最终 head 六次
调用的形状变化，其余每 stream 的 kernel 序列不变；最终接受。
44K 捕获存在首请求/后续请求差别，不据初始化 API 差异宣称收益。

## 已完成前置：省去长文本首块未消费的输出头

基于 f41adfc，仅调整普通文本 chunk 调度的 logits 需求：中间块
（含首块）传 nullptr，最后块保持原计算与末行读回。RunLayers 已
支持空 logits，并在输出头之前完成层状态与可选 trunk 输出；
HeadForward 只读 trunk、写共享 scratch/logits，不更新序列状态。
本轮不改变末块 GEMM 形状，MTP/视觉/内联回退保持现有路径。
证据 prefill-first-head-skip-20260921，必要构建后首项完整 HTTP，
双参考与调度 HTTP 门禁后才检查时间线。首项质量 11/11、
输出对照通过、服务退出 0，
完整五档 HTTP/输出 15/15、服务退出 0，双参考无不利范围分离。
44K TTFT 均值 30.628→30.489 秒，200K 159.698→159.372 秒但
范围重叠，不宣称稳定收益。并发相邻对照 18 请求通过、服务退出 0，
总完成时间无不利分离，与后侧父版范围重叠；约 5.75 秒响应间隔
仍存在。四项生命周期 HTTP 已通过，门禁后源码/库摘要核对通过，
44K 时间线确认首块少 6 次 kernel、2 对分配/释放，decode 调用
不变；全部 forward 的每 stream 序列核对通过。最终接受，固定
参考不更新；末块按需行选择仍待实现，未减少 logits 预留容量。
[P7 消费者与行需求](../dataflow-engine/PREFILL_HEAD_DEMAND.md) 明确
首块省略只是前置，最终块按需计算行与批处理每序列选择仍待实现。

## 已完成前置：普通文本长 prefill 调度

基于 af8fad4（运行时 ec64259），长文本请求进入 chunk 队列，每轮
在已选 decode 后最多推进一块，检查 stream 完成后轮转；末块在锁内
读回 logits 并唤醒请求线程。停止/错误不重新排队，等待线程持有
输入、seq 与 host 输出。调度结果已检查完成，不在请求线程重复同步
随后可能提交的其他工作。正常块大小、精度、首/末块 head 策略不变。
零警告构建后首项质量 HTTP 11/11、输出对照通过、服务退出 0；
完整单流五档 HTTP/输出 15/15，双参考无不利范围分离；
长短请求父版→候选→父版各三轮并发 HTTP 通过，输出一致，
总完成时间与两侧范围重叠；短 TTFT 约 24.6→4.75 秒，但最大
响应 chunk 间隔仍约 5.75 秒。轮转/复用、停机及错误 HTTP 四场景
已通过；门禁后 44K 时间线全部 kernel 次数不变，五个中间 chunk
增加完成同步。无 kernel 关联的 driver API 记录差异保留，不作为
收益。冻结源码、二进制与库摘要一致，原始响应及服务终态最终审查通过。
按单流持平、固定双请求首响应改善接受；长请求 TTFT 增加约 0.9 秒，
不宣称所有请求均变快或任意并发收益。
证据 prefill-chunk-scheduler-20260921；计划见
[chunk 调度](../dataflow-engine/PREFILL_CHUNK_SCHEDULING.md)。

## 已完成前置：文本 prefill 分块序列接口

基于 18f1abe，ModelPrefillTextChunk 统一使用 seq_id、位置与 PLE
历史；每块成功排队后推进 host 游标，最后块才进入 decode，forward
失败进入 kFailed，必须重置后再使用。serve 长 prompt 改用此接口，
移除结束时手工修正整段历史，保留原分块大小、首/末块输出头策略。
这是 stream 有序的 host 状态，不是 GPU 完成事件或故障回滚。
必要构建后首项完整 HTTP，未做前置专项。
首轮质量请求因补充旧 ModelPrefill 的部分 prompt 拒绝检查而主动
中止，记录保留，未计通过。修正后零警告重构建，首项质量 HTTP 11/11、输出对照通过、服务
退出 0；完整五档 HTTP/输出 15/15，对父版及固定参考均无不利
范围分离。门禁后 8,791,040 个 BF16 输出/主干逐位一致，14 项拒绝
检查、2 次同步错误返回注入及重置恢复通过；44K 双版本时间线
全部 kernel/CUDA API 次数相同，按性能持平接受。未验证真实 GPU
故障、并发隔离、MTP 或调度公平性。
[报告](PREFILL_CHUNK_SEQUENCE_2026-09-21.md)，
当前证据 prefill-chunk-sequence-v2-20260921。

## 已完成前置：serve GPU 结果读回提交边界

基于 ede4e09，修复 prefill/普通 decode 在 D2H 或 stream 同步失败后
仍可能发布 host 结果的问题。保留正常路径的计算、复制和同步位置，
检查错误并标记 GPU 不健康，失败走已有请求终止路径。零警告构建后
首项质量 HTTP 11/11、完整五档 HTTP/输出 15/15，服务退出 0；
对父版及固定参考无不利范围分离。门禁后 12 组父版/候选错误返回
注入通过；4K 时间线全部 kernel/CUDA API 次数不变，按性能持平
接受。未验证真实设备损坏、streaming 故障、强制多请求组批或 MTP。
这不是完整序列事务或 MTP 故障恢复。
[报告](SERVE_READBACK_COMMIT_2026-09-21.md)，证据 serve-readback-commit-20260921。

## 已完成前置：线性注意力 scratch 作用域所有权

基于 40b7dbb，六块独立中间缓冲收拢为非复制作用域对象，移除 29 处
手工清理。保留分配尺寸/顺序、同 stream 释放顺序、kernel 与所有状态
更新；不合并分配，不预设性能或容量收益。已零警告构建，质量 11/11、五档 HTTP/输出 15/15，但 4K decode
和 44K TTFT 对父版不利范围分离，初始性能未接受。两档旧→新→旧复核与两侧范围均重叠，
异常未重现，完整门禁现通过。源码所有权核对通过，除 owner/清理
外计算和分配代码完全一致；4K HTTP 所有 kernel/分配次数与父版一致。
按性能持平、清理逻辑集中接受；未做 CUDA 失败注入，不宣称提速。
[报告](LINEAR_SCRATCH_OWNER_2026-09-21.md)，证据 linear-scratch-owner-20260921。

## 已完成前置：GRRead 成对 BF16 gate+mix

基于 79dd7a6，固定 hc=4/hs=2560 的 gate+mix 每线程处理相邻两元素，
合并读取/输出，保持各元素 FP32 累加顺序、BF16 舍入及 CTA 输出数。
质量 11/11、五档 HTTP/输出 15/15；初始 200K decode 不利分离，经
旧→新→旧相邻复核与两侧父版均重叠，原始异常保留。

门禁后 25 组、108620800 个 BF16 输出及实际 GR 链路 75 组逐位通过，
4K 时间线确认 prefill gate+mix 130.558→79.030 ms；kernel/分配次数
不变。正式 4K/8K/44K/200K TTFT 改善约 1.5%–2.0%，decode 按持平
理解；profile 中 decode 局部略慢记录保留，不宣称所有形状均加速。
编译 REG=33、SHARED/STACK/LOCAL=0。固定参考 fcb5925 不变。
[报告](GRREAD_PAIR_MIX_2026-09-21.md)，证据 grread-pair-mix-20260921。

## 已完成前置：GRWrite→下一 GRRead 归一化融合

基于 dd1da64，融合 attention Write 与 MLP Read 的 grouped RMSNorm，
combined 仍写回供后续残差计算。首版相邻 HTTP 仍有 200K 不利范围，
未接受且未做底层测试，证据保留。第二版仅改 residual/block 向量读取，
保持 BF16 舍入与归约顺序，未对齐及非目标形状走分离路径。

第二版质量 11/11、性能 15/15、双参考范围门禁通过；门禁后 75 组、
5,865,523,200 个有限 BF16 值逐位一致，252 布局/504 容量/1260 错误
合同拒绝通过。4K 时间线确认每 forward 净少 48 次 kernel，其他调用
与异步分配数不变。正式 TTFT 约改善 1.5%–2.0%，decode 按持平理解。
编译资源 REG=38、SHARED=21504、STACK/LOCAL=0；不宣称实测 LPDDR
流量或峰值内存改善。固定参考 fcb5925 不变。
[报告](GRWRITE_READ_FUSION_2026-09-21.md)，第二版证据
`.q4t-work/e2e/grwrite-read-vector-20260921/`。

## 已完成前置：decoder 资源合同

基于 81cdbf8，导出 runner 实际布局，13 个视图绑定真实 offset/bytes
与 7 个粗粒度存活阶段。首项质量 11/11、五档 15/15、双参考范围门禁
通过；门禁后 252 布局、504 容量、1260 错误合同拒绝通过。forward
实现及旧 20 个布局字段不变，4K kernel/分配调用数不变，性能持平。
导出 3276 个视图供检查，不是完整执行器，也不覆盖外部权重/序列状态。
[报告](DECODER_RESOURCE_CONTRACT_2026-09-21.md)，证据
`.q4t-work/e2e/decoder-resource-contract-20260921/`。

## 已完成前置：GRFrame gate 工作区管理

基于 e550a02，gate 放到独立 workspace 尾部，跨子层有效，并按 stream
顺序由两个 Read/Write 复用。质量 11/11、五档 15/15、双参考范围门禁
通过；门禁后 30 组逐位、252 布局/504 容量及 4K 时间线通过。
异步分配/释放各 362→266/步，kernel 调用数不变；8192 分块 workspace
2347958272→2348023808 字节（+64 KiB）。性能持平，不宣称峰值改善。
[报告](GRFRAME_GATE_2026-09-21.md)，证据
`.q4t-work/e2e/grframe-gate-arena-20260921/`。

## 已完成前置：GRRead down/up 工作区迁移

基于 c8e8331，down/up 与 normed 作为互不重叠视图借用 MoE 工作区，
按真实 lowrank 预算，gate 所有权保持。首项质量 11/11、五档 15/15，
相对父版及固定参考无不利范围分离；门禁后 30 组逐位、252 布局及
504 次链接容量核对通过。4K 时间线确认异步分配/释放各 554→362/步，
所有 kernel 调用数不变。模型 workspace 保持 2347958272 字节。
接受此阶段，性能按持平理解，不宣称整机峰值或稳定吞吐提升。
[报告](GRREAD_SCRATCH_2026-09-21.md)，证据
`.q4t-work/e2e/grread-scratch-20260921/`。

## 已完成前置：normed 借用 MoE 区域

基于 cd1a624，normed 借用 MoE 工作区，最大容量预留，HC GEMM scratch
和输入输出仍独立。构建零警告，首项质量 11/11、五档 15/15；初始
44K decode 相对父版的不利分离，经旧→新→旧 HTTP 复核未重现，候选
TTFT/decode 同时与两组旧对照重叠。原始异常与复核完整保留。
门禁后 84 布局、168 链接容量核对通过，4K 各 kernel/分配次数不变。
8192 分块共享 workspace 2515730432→2347958272 字节，再减 160 MiB；
相对两份 normed 阶段累计减 320 MiB。未测系统峰值，不宣称稳定提速。
[报告](NORMED_MOE_ALIAS_2026-09-21.md)，证据
`.q4t-work/e2e/normed-moe-alias-20260921/`。

## 已完成前置：workspace 布局单源化

基于 657b2e8，DecoderWorkspaceLayout 统一容量与偏移，公开容量查询
和 forward 共用一个构造函数，消除重复维护；区域仍然独立。
构建零警告，首项质量 11/11、五档性能 15/15，输入输出一致、退出 0；
相对直接父版与固定参考均无不利分离范围，正式参考保持不变。
门禁后 84 个布局、各 14 字段与父版相同，168 个实际链接容量值相同；
4K 时间线各 kernel 调用数、分配数也不变。保留父版容量，不宣称提速。
[报告](DECODER_WORKSPACE_LAYOUT_2026-09-21.md)，证据
`.q4t-work/e2e/decoder-workspace-layout-20260921/`。

## 已完成前置：复用 normed 工作区

基于 7a34dd3，两个 GRRead 共用一份 d_normed，预算与实际 carve
同步删除第二份区域。构建零警告，首项质量 11/11、五档性能 15/15；
输入输出一致，服务退出 0。对直接父版和固定参考均无不利分离范围。
200K decode 本轮均值 17.119 tok/s，小样本有利变化不作为稳定提速主张。
门禁后编译容量核对 21 组层预算及 7 组模型最大值通过：8192 分块的
共享 workspace 2683502592→2515730432 字节，减少 160 MiB，未测系统
峰值。4K 时间线全部 kernel 调用与分配次数不变。
[报告](GRFRAME_NORMED_REUSE_2026-09-21.md)，证据
`.q4t-work/e2e/grframe-normed-reuse-20260921/`。

## 已完成前置：GRFrame read/write 第一阶段

基于 fcb5925，主层 attention/MLP 接入 GatedResidualFrame：Read 生成
mixed 与 gate，Write 只消费 residual、gate、子层输出，不保留 normed。
旧 head/MTP API 保留；两个 normed 区域与 arena 容量暂不改变。
构建零警告，首项质量 HTTP 11/11、五档性能 15/15，输入输出一致；
全部 TTFT/decode 重复范围与基线重叠，性能按持平理解。
门禁后 30 组、2215864320 个有限 BF16 值逐位一致；覆盖 normed 后
Write 正确，frame 状态检查通过。4K 时间线全部 24576 个 GR 子层
确认 gate 前移，kernel/分配数量不变。未证明提速、容量下降或净复杂度
下降；这是后续生命周期复用的已验证边界，不是完整引擎。
[阶段报告](GRFRAME_READWRITE_2026-09-21.md) /
[合同](../dataflow-engine/GRFRAME_RUNNER_PLAN.md)，完整证据
`.q4t-work/e2e/grframe-readwrite-20260921/`。

## 正式性能参考

正式五档性能参考仍为 **fcb5925**（短路径 top-k 寄存器网络）。
最新运行时为普通文本单序列末行输出头，正式参考不重置。
质量 11/11、性能 15/15，输出摘要一致、服务退出 0，构建零警告；
完整门禁后 352 组、277598448 个槽/长度逐位一致，4K HTTP 时间线通过。

| 输入 token | TTFT 均值秒 | decode tok/s |
|---:|---:|---:|
| 1024 | 0.830 | 18.611 |
| 4096 | 2.794 | 17.871 |
| 8192 | 5.488 | 18.161 |
| 45056 | 31.856 | 17.855 |
| 204800 | 165.023 | 17.072 |

4K decode 较前一版 +0.76%，8K TTFT -1.08%，其余范围重叠。
正式参考见 tools/evalscope/fixtures/performance_reference.json；
二进制 SHA 与完整边界见 [报告](SHORT_TOPK_2026-09-21.md)。证据
`.q4t-work/e2e/short-topk-register-20260921/`。build/q4t 对应已接受的单序列末行输出头版本；
已接受 46b732b 二进制保存在 prefill-last-row-20260921/q4t-before；
已接受 f41adfc 二进制保存在 prefill-first-head-skip-20260921/q4t-before；
已接受 ec64259/af8fad4 二进制保存在 prefill-chunk-scheduler-20260921/q4t-before；
已接受 18f1abe 二进制保存在 prefill-chunk-sequence-v2-20260921/q4t-before；
已接受 ede4e09 二进制保存在 serve-readback-commit-20260921/q4t-before；
已接受 40b7dbb 二进制保存在 linear-scratch-owner-20260921/q4t-before；
已接受 79dd7a6 二进制保存在 grread-pair-mix-20260921/q4t-before；
已接受 dd1da64 二进制保存在 grwrite-read-fusion-20260921/q4t-before；
已接受 81cdbf8 二进制保存在 decoder-resource-contract-20260921/q4t-before；
已接受的 e550a02 二进制保存在 grframe-gate-arena-20260921/q4t-before；
已接受别名版保存在 grread-scratch-20260921/q4t-before；
已接受的 cd1a624 二进制保存在本轮证据目录 q4t-before。

## 后续演进

普通文本长 prefill chunk 调度已接受，完整单流、固定双请求对照、
轮转/复用、停机与合成错误 HTTP，以及门禁后时间线通过。见
[执行与验收报告](../dataflow-engine/PREFILL_CHUNK_SCHEDULING.md)。
单序列末行输出头已接受；下一步实现批处理每序列输出行选择，
保持精度并独立完成完整 E2E 和真实多请求门禁，再核对数值。

GRFrame、单 normed 复用、布局单源化与 normed/MoE 别名已接受。
GRRead down/up 暂存迁移已接受。gate 独立工作区也已接受。资源合同与 GRWrite→下一 GRRead 融合第二版已接受。
成对 gate+mix 也已接受。下一步结合整体时间预算与资源合同推进
模型专用执行计划，独立候选仍先
完整 HTTP。完整 D/P/S、可执行计划和状态提交仍未实现。
[GRFrame 合同](../dataflow-engine/GRFRAME_RUNNER_PLAN.md) 与
[JSON 清单](../dataflow-engine/plans/grframe_main.json) 区分提案和候选绑定；
完整引擎进度见 [筹备状态](../dataflow-engine/STATUS.md)。

## 待处理

1. 随机文本的工具与服务端分词计数差异（1024 目标实际 932/921/915）未解决。
2. 补 messages/真实语料及多轮对照。原生模板渲染后的 prompt 检索题通过
   11/11，不证明 messages 模板转换、多轮、多模态或全面长上下文召回正确。
3. 断连专项已通过六轮恢复 E2E：旧版 SIGPIPE 已修复，FD、请求计数、
   输出与正常退出符合预期，见 [断连报告](DISCONNECT_FIX_2026-09-20.md)。
   后续发现旧版 4K 输出非确定性，固定稀疏索引槽位后五档各三次输出稳定，
   见 [复现报告](REPRODUCIBILITY_2026-09-20.md)。再做质量题发现 QSA 数学
   错误，本轮已修复并恢复 11/11 精确正确。仅固定顺序的中间版曾有约
   1%–2% decode 回退；数学修复阶段和本次等价简化的差异分别见上，历史
   固定矩阵速度目标现已由位置元数据改动追回，见最新进展。
4. 纯 prefill 阶段时间尚未测量；TTFT 不能替代它。下一步在 E2E 已通过的
   请求上进行必要细分析，须先满足现行门禁；不依据历史 bench 决定方向。
5. [静态预算](DATAFLOW_OPTIMIZATION.md) 约 10.24 GB/token，260 GB/s 对应
   25.4 tok/s，是理想参照，不是已测性能。“整个 decode 已无优化空间”没有
   当前 E2E 与完整时间账支持。

## 历史与证据入口

已完成阶段从本入口移至 [2026-09-21 状态快照](HISTORY_STATUS_2026-09-21.md)，
保留数学修复、五档基线、成功优化和撤回实验的原记录；不重复充当当前状态。
每次实验的详细报告见 [文档索引](README.md)，日志只追加至 [docs/log/](log/README.md)。
[治理前状态](HISTORY_STATUS_2026-09-20.md)、
[数据流分析快照](HISTORY_DATAFLOW_2026-09-20.md)、
[旧入口](HISTORY_AGENT_ENTRY_2026-09-20.md) 均为历史材料，不能替代当前 E2E。

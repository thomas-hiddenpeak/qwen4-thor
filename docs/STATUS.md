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

## 最新接受：GRWrite→下一 GRRead 归一化融合

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
最新运行时为上述融合第二版阶段，正式参考不重置。
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
`.q4t-work/e2e/short-topk-register-20260921/`。build/q4t 对应已接受的向量读取融合第二版；
已接受 dd1da64 二进制保存在 grwrite-read-fusion-20260921/q4t-before；
已接受 81cdbf8 二进制保存在 decoder-resource-contract-20260921/q4t-before；
已接受的 e550a02 二进制保存在 grframe-gate-arena-20260921/q4t-before；
已接受别名版保存在 grread-scratch-20260921/q4t-before；
已接受的 cd1a624 二进制保存在本轮证据目录 q4t-before。

## 后续演进

GRFrame、单 normed 复用、布局单源化与 normed/MoE 别名已接受。
GRRead down/up 暂存迁移已接受。gate 独立工作区也已接受。资源合同与 GRWrite→下一 GRRead 融合第二版已接受。
下一步结合已验收时间线核对 GRRead 数据流与生命周期，独立候选仍先
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

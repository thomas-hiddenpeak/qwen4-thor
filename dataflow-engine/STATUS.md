# QTDE 筹备状态

更新：2026-09-21。只描述本目录的设计工作，不替代现有 runner 状态。

## 已确立原则

保持当前精度、不造成性能回退时，降低系统复杂度可以独立构成采纳
理由，无需同时加速。适用于 runner 与 QTDE。按
[验证标准](VALIDATION.md#采纳原则复杂度降低是独立收益)记录精度保持、
性能不回退与复杂度净减少的证据；研究原型和正式替换的验收分开。

## 已完成

- 阅读技术报告的架构章节，并与当前主干、PLE、MoE、MTP 代码对照。
- 定义 GRFrame、微块 Selection、sequence generation、推测 epoch。
- 推演 D：单序列 decode；P：长 prompt 分块 prefill；S：多序列 MTP。
- 明确 shifted draft 与主模型消费/输出游标、raw tail 回滚、I/O 槽生命周期。
- 给出容量公式、权重逻辑字节账、MTP 完整快照与更新日志的空间比较。
- 制定数值分级、边界用例、性能测量与阶段原型路线。

## 暂定决策

| 决策 | 当前选择 | 重新评估依据 |
|---|---|---|
| 名称 | QTDE / 模型专用数据流引擎 | 用户偏好，尚未作为正式发布名称 |
| 初版精度 | checkpoint NVFP4 + BF16 dense/activation + FP32 SSM | 匹配参考质量与带宽账 |
| 顶层对象 | GRFrame 与版本化序列状态 | 三计划均能表达，无隐藏所有权 |
| 执行映射 | 预分配 arena + 多 kernel/graph；局部探索 persistent | 每 shape 的正确性与关键路径 |
| prefill | chunk-major，完整 chunk 边界调度 | 权重重读与 TTFT/TPOT 的实测权衡 |
| MTP | greedy；固定 cohort 内统一 k | 先证明提交边界，再扩 ragged/采样 |
| MTP 恢复 | 起点和逐位置完整 checkpoint | 更新日志正确性及真实接受率下成本 |
| 输出头 | 指定行 / greedy token 为独立需求 | 需要完整 logits 或采样时换计划 |
| 压缩 index | 长期 compressed table + raw tail | 压缩边界、拒绝回滚和短长度评分范围测试 |

## 未验证

- 完整 GRFrame/R0/R1 执行计划仍未实现；MoE D5 固定形状子链已在 runner
  实现并取得首轮 E2E 证据，不等同于完整引擎已经完成。
- 硬件实际资源、选定 chunk/tile/CTA 数、graph 映射、精度阈值待定。
- 更新日志、FP8 residual、draft 索引复用是实验，不是默认承诺。
- 不把所有阶段的“权重只读一次”或片上全驻留作为设计假设。

## 新增源码与 API 核查

[MoE GPU 执行合同](MOE_DEVICE_PLAN.md) 将计划 D5 落到当前 runner：
先以 M=1/top-10 固定形状闭合 device 路由→grouped GU→量化→grouped
DN→固定 slot combine。核对了本机 cuBLAS 13.5.1 的 grouped 接口、
scale 独立 atom 容量、现有 host counts 的消费者和数值边界。
独立 CUDA 路径已实现：首轮质量 11/11、五档性能 15/15，decode 提升
6.71%–7.65%，TTFT 范围重叠；随后 707520 个数值逐位一致。
配对 1K HTTP 确认每步 counts 回读 48→0、kernel 3561→1737，
分配/释放仍各 554 次。没有前置能力 probe 或专项测试。
最终格式整理重构建后独立完整复验通过，decode 提升 6.65%–7.55%，
数值逐位一致、时间线计数与首轮一致，正式参考已更新。
详见 [实现报告](../docs/MOE_DEVICE_DECODE_2026-09-21.md)。

## 下一步

MoE 已提交推送 98206de；[QSA decode 输出维度拆分](QSA_DECODE_PLAN.md)
已接受：完整 HTTP 及逐位专项通过，decode 提升 1.31%–3.52%。
这是 D3 QSA 固定形状子链的实现，不等于完整 GRFrame 执行计划完成。
R0/R1 的可机读计划和 GRFrame 完整闭环仍未实现，不能由单个 MoE
子链代替。后续按新时间线重新分配权重读取、计算、host 控制与临时空间
预算，不把 kernel 数量减少当作所有剩余阶段收益的保证。

## 当前推进：decode 多级选择

已接受版 200K 时间线定位多级 top-k 累计约 4.121 ms/decode 步。
[decode top-k 计划](DECODE_TOPK_PLAN.md) 已接入独立 CUDA 单元，
复用私有寄存器比较网络，构建零警告，完整 HTTP、数值专项及时间线通过。
200K decode +4.98%，未改变 score、同分规则或候选覆盖；本轮接受。
[短上下文索引打分](INDEXER_DECODE_PLAN.md) 也已接受：完整 HTTP、
589824 个分数逐位及配对 4K 时间线通过，4K decode +6.20%。
[短路径 top-k 寄存器网络](SHORT_TOPK_PLAN.md) 已接受，完整 HTTP 后
精确选择与 4K 时间线通过，4K decode +0.76%、8K TTFT -1.08%。

## GRFrame 架构入口（第一阶段接受）

[GRFrame runner 合同](GRFRAME_RUNNER_PLAN.md) 核对 normed 与原始
residual 的区别，分开 inject 提前、生命周期合并和 arena 别名三个阶段。
top-k 与第一阶段 read/write 已接受；不能把两份 160 MiB
区域直接写成峰值节省。

GRFrame 单子层已有首份 [JSON 合同](plans/grframe_main.json)，标为
proposal_not_executable，含源码指纹、buffer 字节与节点顺序；无执行器
或 schema 验证器，不替代 R0/R1 完整计划；runner 的实际改动见下。

GRFrame 第一阶段已接入 runner：Read 提前 gate，Write 消费小帧，
两个 normed 区域保留；质量 11/11、五档性能 15/15 与门禁后 30 组
逐位及 4K 顺序核对通过，第一阶段接受。性能持平、容量未减。
JSON 仍不是可执行计划，runtime_candidate 字段单独记录候选状态。

第二阶段已接受（基线 7a34dd3）：两次 GRRead 共用 d_normed，预算与
carve 同步少一份 T*10240*2 区域；构建零警告，首项质量 HTTP 11/11
通过、服务退出 0，完整五档性能 15/15、门禁后时间线和编译容量核对
通过。8192 分块共享 workspace 减少 160 MiB，无系统峰值实测结论。
容量与 offsets 单源化及 arena 别名的后续结果见下。

workspace 布局单源化已接受（基线 657b2e8）：容量查询与 forward
共用值类型布局构造，保留区域独立；构建零警告，首项质量 11/11 通过，
完整五档性能 15/15 与门禁后 84 个布局/168 个链接容量值核对通过，
4K kernel/分配计数保持。此阶段尚未引入 arena 别名。

normed/MoE 别名已接受：质量 11/11、五档及 44K 旧→新→旧复核通过，
门禁后 84 布局/168 容量通过，模型 workspace 再减 160 MiB；初始异常
保留，未宣称稳定提速。下一步 down/up 暂存，gate 所有权独立处理。

## GRRead down/up 已接受

基于 c8e8331，[暂存合同](plans/grread_scratch.json) 定义 normed/down/up
三个互不重叠的视图及 MoE 覆写顺序，容量按实际 lowrank 计算。
完整 HTTP、逐位、布局及 4K 时间线通过，每步异步分配/释放各
554→362，共享 workspace 不变。详见
[报告](../docs/GRREAD_SCRATCH_2026-09-21.md)。下一步独立处理 gate
的跨子层存活期，不能借用随后由 MoE 覆写的区域。

## GRFrame gate 独立工作区已接受

[gate 合同](plans/grframe_gate.json) 已落实到 runner，gate 存储与所有
子层暂存不重叠，两个 frame 按 stream 顺序复用。完整 HTTP、逐位、布局
与时间线通过，每步异步分配/释放各 362→266，workspace 增加 64 KiB。
性能持平，正式参考不变。[报告](../docs/GRFRAME_GATE_2026-09-21.md)。
下一步把实际布局和 GR 执行依赖绑定成可检查描述；完整 D/P/S 执行器、
状态提交和其他子层 arena 化仍未完成。

## 实际布局的资源合同已接受

基于 81cdbf8，decoder_workspace.h 导出实际布局；13 个 workspace
视图绑定真实偏移/字节与 7 个粗粒度阶段。完整 HTTP 后，252 布局、
504 容量、1260 错误合同拒绝及 4K 时间线通过。旧位置、总预算、
forward 和调用次数不变，性能持平。
[报告](../docs/DECODER_RESOURCE_CONTRACT_2026-09-21.md)。
存活语义仍需代码审查；本描述不覆盖外部权重/状态，不是执行器，
不保证缓存驻留。下一步按 R1 路线审查 GRWrite→下一 GRRead 衔接。

## 已接受：GRWrite→下一 GRRead 归一化融合

[融合合同](plans/grwrite_read_fusion.json) 将 attention Write 与下一次
MLP Read 的 RMSNorm 合并，combined 仍写回供下一次 residual 使用。
首版相邻复核 200K 对前侧父版不利、与后侧重叠，未接受且没有做底层
测试。第二版改为对齐向量读取，BF16 舍入和归约顺序保持。

第二版质量 11/11、性能 15/15、双参考范围门禁通过；门禁后 75 组逐位、
252 布局/504 容量/1260 错误合同拒绝及 4K HTTP 时间线通过。每 forward
净少 48 次 kernel，容量和异步分配次数不变。正式 TTFT 改善约
1.5%–2.0%，decode 按持平理解；编译 shared 21504 字节，区别于源码
staging 20480 字节。证据 grwrite-read-vector-20260921，首版另存。

这仅验证普通单流固定模型形状下的衔接，不代表完整执行器、MTP/多流、
LPDDR 流量或峰值内存改善。下一步继续从已验收时间线核对 GRRead
数据流和生命周期，独立改动仍先完整 E2E。

## 已接受：GRRead 成对 gate+mix

固定模型形状下相邻两个 channel 共用一个线程，保留每个元素原数学。
完整 HTTP、200K 相邻复核、逐位与 4K 时间线通过；prefill 局部累计
130.558→79.030 ms，TTFT 改善约 1.5%–2.0%，kernel/分配/容量不变。
Decode 局部略慢而 E2E 范围重叠，按持平接受，原始异常保留。该物理
映射改进不代表完整执行器，下一步仍需整体时间预算与资源合同驱动。
详见 [报告](../docs/GRREAD_PAIR_MIX_2026-09-21.md)。

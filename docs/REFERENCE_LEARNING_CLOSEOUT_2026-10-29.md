# 五个参考项目学习与特性吸收：阶段收尾

2026-10-29，按用户决定结束本轮 KVMem、NInfer、ActKV、Strata 和
Megatron Inference 的源码研究与适用特性吸收。核对起点为主线
`5fcd159`，包含完整会话缓存、显式公共前缀共享及 agent 接入示例。
**本轮已完成；剩余候选按实际需求重新立项，不是待补交付清单。**

本文更新[原综合计划][original-plan]的执行状态与优先级，不改写原始
失败、NO_GO、统计限制或工程接受决定。这里的“吸收”包括方法、状态
合同、测试设计和按本项目约束实现的能力，不表示整套移植、运行时依赖，
也不把本项目原有能力重新算作外部项目带来的新增实现。

## 1. 实际研究对象与固定源码

下表是实际读过的版本，不代表上游最新版本。本轮收尾没有更新上游、
构建或运行外部引擎；外部性能自报不能替代 Thor 本机验收。

| 项目 | 实际审阅版本 | 关键实现与所得认识 |
|---|---|---|
| [KVMem][kvmem] | `0af10ece3d78edb905c62357e52a62147df69d58` | [存储事务][kvmem-transaction]的 prepare/spill/restore/publish/abort、`host_kv_storage.cpp` 的 lease/预算/失败处理、[LRFU 状态][kvmem-lrfu]的 preview/commit/checkpoint；完整会话身份与失败边界比直接移植检索更有价值 |
| [NInfer][ninfer] | `81c8ce093b2c1646a87566a8e59d807fcf0ec95c` | [MTP Forward/Finish][ninfer-mtp]与 device 元数据、`planning/graph_profiles.cpp` 的执行包络、[proposal head 筛行][ninfer-proposal]与加载/argmax remap；借鉴执行组织与草稿成本拆分 |
| [Strata][strata] | `296b59d2f73c8f04a805df29c2fa97655adace78` | [suffix proposal][strata-suffix]、[混合模型会话状态][strata-session]与物理槽所有权、[QSA 页驻留][strata-kv]；历史提问中的“Strada”即此项目 |
| [ActKV 社区实现][actkv] | `fe4c5d9d57f6714f2ec1d6657d237813d7ec1a89` | [LRFU 更新][actkv-lrfu]、`confidence_monitor.cpp` 与 adapter 的预算增长；它与下行实现的评分定义、执行方式不同 |
| [actkv-open][actkv-open]（ActKV 补充审阅） | `0345bd76123f78b0c6ce525a28db8cb7d4e271dd` | 非官方实现；[ActionLRFU][actkv-action]、[实际 HF KV 删除][actkv-session]、`kernels/compaction.py` 的搬移计划；不将不同实现或论文概念视为一个已验证算法 |
| [Megatron-LM 的推理实现][megatron] | `3e2807ce5e787f81b7275be2383092fe0c3bfc71` | [固定 IDs/当前 scores][megatron-router]、[元数据源完成协议][megatron-metadata]、[数值负控][megatron-test]和调度生命周期；没有移植其分布式执行栈 |

KVMem、NInfer、Strata、ActKV 两份代码的本地只读检出保留在
`.q4t-work/reference-review-20261016/`；Megatron 位于
`.q4t-work/megatron-review-20261016/megatron-lm/`。
旧源码评估和综合计划的版本化恢复入口见[研究归档](ARCHIVED_RESEARCH.md)。
本地大文件与原始测量仍按原路径保留，不能因本轮收尾删除。

## 2. 已吸收能力及当前边界

### NInfer：执行组织与独立草稿 head

- **B1a 精确位置下传**：从已知逻辑位置计算真实最大值，传给 MTP
  forward，减少冗余回读；不能用容量或近似上界代替真实位置。
  已有限工程接受，统计性能证据不足，原性能条件没有追认通过。
- **B1b scratch 复用**：extend gather offset 改用预留缓冲，消除每次
  非空 extend 的临时 `cudaMalloc/cudaFree`，明确资源生命周期。
  已有限工程接受；**提速、持平和统计非劣没有因此得到证明**，也不是
  后续 Megatron 诊断的前置条件。
- **C1 独立草稿 head**：固定 65536 行 BF16，验证映射后将局部 argmax
  还原为全局 token ID；保留完整 target head 和 verify。显式启用，
  新增常驻逻辑内存 320.25 MiB。未移植量化 head，也不承诺任意输入
  与完整草稿 head 的最终输出逐位相同。

代码入口：[MTP](../src/mtp/mtp.cu)、[接口与 scratch](../include/q4t/mtp/mtp.h)、
[子集加载](../src/mtp/subset_head.cpp)。接受依据：
[B1a 合入](MTP_POSITION_B1A_MERGE_2026-10-18.md)、
[B1a 部署](MTP_POSITION_B1A_DEPLOY_2026-10-18.md)、
[B1b 独立决定](MTP_GATHER_B1B_2026-10-19.md)、
[子集验收](MTP_SUBSET_RESUME_2026-10-23.md)与
[子集主线合入](MTP_SUBSET_MAIN_MERGE_2026-10-23.md)。
子集首轮 NO_GO 保留，后续修复验收没有追认失败版本。

### Megatron：路由诊断、元数据所有权和可靠性

- **A1 固定 IDs 诊断**：独立 CLI/API 在真实 MoE 选择点指定有序专家
  IDs，权重仍从当前 logits 计算；分别比较 hidden、logits、IDs、weights
  与 output。固定路由不等于冻结权重、跳过 gate 或证明剩余算术正确。
- 后续加入真实文本主干层输入捕获、同形状重放和行重排/无关行负控。
  已测真实文本捕获限于层 2 和有限行，不能写成全层、全生成过程或
  MTP 全角色路由重放已经实现。
- 生产侧吸收元数据上传源保活、完成后复用、早退/失败处理；进一步
  加固受测权重加载入口，并验证调度提交、取消、槽位和请求 ID 复用。
  这些是可靠性工程，不是已证明的性能优化或全仓异步安全证明。

代码入口：[独立重放](../tools/verify/moe_replay/README.md)、
[真实输入捕获](../tools/verify/moe_replay_real/README.md)、
[元数据批次 owner](../src/model/metadata_upload_batch.cpp)、
[调度提交合同](../tests/server_scheduler_submission_test.cpp)。结果见
[A1](MOE_REPLAY_A1_2026-10-19.md)、
[真实输入](MOE_REPLAY_REAL_INPUT_RESULT_2026-10-19.md)、
[元数据接受与部署](METADATA_BATCH_DEPLOYMENT_2026-10-20.md)和
[可靠性结果](MEGATRON_RELIABILITY_RESULT_2026-10-20.md)。

### Strata：固定 k3 suffix 与会话恢复思路

已实现固定 k3 suffix proposal，仍由原 target verify 决定接受，失败或
不符合准入条件时使用神经草稿；已提交历史与临时 proposal 分开管理。
同时完成不等长槽位、长主 agent 与三个子代理的适用配置及状态合同。
完整 head/MTP k3 仍默认，suffix 与子集分别显式启用。

代码入口：[suffix drafter](../src/mtp/suffix_drafter.cpp)、
[请求集成](../src/server/chat_generation.cpp)。交付见
[固定 k3](MTP_SUFFIX_DELIVERED_2026-10-27.md)、
[长短 agent](MTP_SUFFIX_AGENT_DELIVERED_2026-10-27.md)与
[主线合入](MTP_SUFFIX_MAIN_MERGE_2026-10-27.md)。
长短 agent 版本存在原收益目标未达标及保护负点，由用户在完整披露后
工程接受；不改写为所有优化门槛通过。会话恢复的共同成果见下节。

### KVMem / Strata：完整状态、SSD 会话缓存与显式前缀复用

原 D1 的实际需求已完成，并扩展到 HTTP 使用：同一进程内，请求结束后
释放执行槽；槽位被其他请求覆盖后，将完整前缀从 SSD 恢复到可用槽。
另有显式公共前缀种子，源请求保存一个指定边界，子代理只读匹配复用。

保存范围包含目标 SSM/短卷积/PLE 卷积、有效 KV、QSA raw/压缩完整组、
RoPE，以及草稿 KV/索引、rolling g、d0、pending token、最后目标 trunk
和全局 token 历史。恢复保留目的槽自己的页表，处理 pending 改变，
suffix 索引由本次已提交历史重建；正常请求完成才发布新快照。

代码入口：[状态枚举](../src/server/session_state.cpp)、
[存储与租约](../src/server/session_cache.cpp)、
[HTTP 生命周期](../src/server/chat_generation.cpp)。适用范围、证据和
回退见[会话缓存结果](SESSION_CACHE_RESULT_2026-10-27.md)、
[显式种子结果](PREFIX_SEED_RESULT_2026-10-28.md)及
[主线收尾](SESSION_PREFIX_MAIN_MERGE_2026-10-29.md)。

必须保留以下区别：

- 缓存是文本 greedy MTP k3 的显式功能；客户端仍传完整历史并精确
  匹配 token。不是任意生成中断点恢复，也不支持服务重启后恢复。
- 释放的是执行槽，**预分配的 GPU KV 内存没有释放**；共享的是完整
  状态快照，恢复后各槽独立，不是物理 KV 页共享或在线冷页调入。
- 存储损坏/非 GPU I/O 失败采用安全 miss、重置并重算。保存前可能
  已淘汰旧缓存，恢复也可能部分写入后失败；不宣称强事务回滚能恢复
  旧缓存和 LRU。GPU 错误不能作为普通 miss 静默继续。
- 独立 GPU 状态参考覆盖四个代表目标层及真实 draft，HTTP 使用完整
  48 层；两者不能合称全模型任意输入逐位证明。原自动多检查点
  prefix 候选 NO_GO、保护负点和统计非劣未建立仍保留。

### ActKV：明确了适用边界，未移植淘汰算法

已读社区 LRFU/置信度预算，以及独立实现的 action 注意力评分与 KV
删除/搬移代码。最值得保留的原则是：**语义策略只根据已提交历史更新，
preview 不重复强化，必要时 checkpoint/restore**。其中清晰的独立状态
实现来自 KVMem 主仓 `lrfu.hpp`，不能把 ActKV fork 的整个 adapter
preview 调用链描述成无副作用事务。

本项目 MTP 接受前缀与 suffix 已有相应状态边界，但没有引入跨轮 LRFU
分数；不为不存在的策略状态再建通用事务框架。当前会话缓存 LRU/TTL
淘汰的是可重算快照，不删除模型逻辑历史。真实发生的读取、命中、搬运
和拒绝草稿成本属于资源事实，不能随 token 回滚而从统计中抹去。

本项目 QSA 已对全部历史候选执行学习到的稀疏选择；额外永久删除候选
会改变未来计算，不能以选中子集的 LSE 估计完整历史重要性。固定容量
数组内的 compaction 也不等于释放统一 RAM。因此 ActKV 研究已完成，
**近似评分、confidence budget、永久 KV 淘汰没有作为运行时特性合入**。

## 3. 原计划处置与重新启动条件

已完成的 A1、相关可靠性负控、B1a/B1b、C1/C2 和 D1 关闭。旧 A2 的
全执行 trace、B2/B3、D2/D3、E/F 不再按编号排队。下面是条件候选，
只有具体问题成立时才选择一个范围；不因上游新增功能自动重启。

| 暂缓项 | 重新启动所需的具体依据 | 首个工作包与不可突破的边界 |
|---|---|---|
| Megatron 式 MTP 全执行 trace / 更广路由捕获 | 有一个现有诊断回答不了的分歧，或明确消费完整路由/成本记录的分析任务 | 先限定层、角色、位置和记录消费者；区分 propose/verify/extend、执行/接受/输出，保留全部真实执行成本，不将采集扩成默认生产开销 |
| GPU accept、device dispatch、有界 CUDA Graph（B2/B3） | 当前身份的 HTTP 基线和后续适用观测显示 host 结算、回读或 launch 等待有可消除的实际成本 | 每次只选一个控制段/稳定子图；保住精确位置、真实 T、接受/回滚、取消与源生命周期，不捕获 PLE 主机 I/O，不用 padding 改变算术形状来凑图 |
| 量化草稿 head、其他子集规模 | 现行 65536 BF16 的实际 head 成本或接受率成为瓶颈，且有独立校准/留出材料和明确目标 | 单一候选，冻结额外 RAM、全局 ID 映射、质量/收益及保护项；完整 target verify 保留，不自动扫描规模或改动态 k |
| GDN Record/Fold / ReplaySSM | 有新机制能解释并解决[既有 NO_GO](ARCHIVED_RESEARCH.md)的失败，且 checkpoint 写入/容量收益足以覆盖重算 | 先说明相对失败方案的实质变化，保护同形状接受前缀、PLE 卷积和数值合同；不原样重跑，也不把外部压缩比当提速 |
| 动态 KV 页池、跨槽物理页共享（D2） | 真实并发/长上下文需求超过现有不等长槽位，且统一 RAM 账证明预分配 KV 或重复页是限制 | 明确可实际归还/共享的字节及页表/引用生命周期；保持逻辑位置和 QSA 压缩组，验证取消与复用，不能只搬移固定数组后宣称省内存 |
| 在线 KV 冷驻留（D3） | D2 或更简单容量方案仍不满足实际目标，并有 SSD 带宽、工作集及 PLE 竞争的有界预算 | 保留完整 QSA 候选索引，选中页全部到齐后 attention；独立解决 prefill 容量与失败恢复，不漏页近似，不把 host 副本当容量卸载 |
| 专家预测预取/缓存策略（E） | 真实 SSD 专家容量需求成立，冷/热缓存和实际 SSD 读取归属已厘清，存在可预测且能掩蔽的 miss | 先选一个策略和容量；完整计算 staging/L2/mirror/页缓存及 PLE 竞争，保护覆盖前 I/O 完成与 slot 所有权；遵守[SSD 合同](SSD_OFFLOAD_CONTRACT.md)，不自动恢复已搁置的共享主机缓存路线 |
| ActKV 策略状态 / 近似淘汰（F） | 仅在引入实际跨轮语义分数时需要策略事务；永久删除则还需明确的容量需求和获认可的质量/容量取舍 | 先单独定义评分与提交状态；近似实验另定质量材料、误差与容量目标，按实际释放 RAM 验收，不用缓存快照 LRU 或上游分数冒充模型无损 |
| 其他显式近似（F）：低比特 KV/K8V4、Strata route prior、缩减 QSA 候选 | 实际容量/计算成本需求成立，并明确接受新的数值与任务质量取舍；当前均未移植 | 每次单独一个候选和独立任务材料，定义质量与真实资源目标；驻留偏置不能伪装成原 router，缩减候选不能称完整 QSA，既有 N=4 合同不能替代新近似验收 |
| 任意生成中途暂停、跨重启会话持久化 | 实际业务要求这些能力，而请求边界缓存无法满足 | 分别冻结进行中请求状态或持久文件格式/身份兼容/恢复协议；它们不属于已完成 D1 的遗留缺陷 |

## 4. 后续工作入口与执行规则

下一步从真实 agent 使用出发。已有
[1 长主 agent＋3 子代理接入说明](AGENT_CACHE_INTEGRATION.md)和
[可运行客户端](../tools/examples/agent_cache_demo.py)，无需再写一份同类
演示。示例完成 CPU 准备与模拟生命周期检查，没有新增真实 GPU/HTTP
验收；预渲染 prompt 的缓存收益不能直接外推到任意 SDK 的 messages、
thinking 或 tool_calls 序列化。

若实际接入出现问题，优先在以下三类中选定一个：历史重渲染导致缓存
miss；保存/恢复在模型锁内的等待成本；真实统一 RAM 或并发容量不足。
先用真实输入、身份和可观察结果明确问题，再选择上表机制。
已接受版本没有新失败或依赖变化时，不重开整轮验收。

重新启动时按当前 [EVALUATION](EVALUATION.md) 与
[性能决策协议](PERFORMANCE_DECISION_PROTOCOL.md)分类：业务/可靠性
工作完成受影响合同和适用 HTTP 质量，性能优化仍以 HTTP E2E 为首测并
完成适用五档对照。事前冻结候选、收益与保护项、资源/请求/墙钟预算、
停止和回退条件；GPU 全阶段经共享独占锁。只复用身份与依赖未变的
有效证据，不把历史某阶段的临时豁免扩为通用规则。

## 5. 本次收尾范围

本次只整理文档、参考归属和条件候选，更新 README、参考说明、状态、
归档索引和日志；不修改运行时代码，不重建、替换默认程序或发布元数据，
不新增 GPU/HTTP 测量。原始证据、NO_GO、工程接受、回退包与路径全部
保留。9 份 Markdown 的文档核对通过：45 个新增本地链接、20 个固定
Git 源码链接/版本及行号范围有效，历史日志原文保留，当前状态锚点唯一，
`git diff --check` 通过且差异仅限文档。按既有授权将本轮变更独立提交
并推送当前有效分支 main；不新增研究分支或自动启动候选。

[original-plan]: https://github.com/thomas-hiddenpeak/qwen4-thor/blob/9a5bc0cfc92e92a8db953092691072fed5e2ede3/docs/REFERENCE_INTEGRATION_PLAN_2026-10-16.md
[kvmem]: https://github.com/kvmem/kvmem/tree/0af10ece3d78edb905c62357e52a62147df69d58
[ninfer]: https://github.com/Neroued/ninfer/tree/81c8ce093b2c1646a87566a8e59d807fcf0ec95c
[strata]: https://github.com/Niko1221/Strata/tree/296b59d2f73c8f04a805df29c2fa97655adace78
[actkv]: https://github.com/melis2023/kvmem-actkv/tree/fe4c5d9d57f6714f2ec1d6657d237813d7ec1a89
[actkv-open]: https://github.com/Maurya30/actkv-open/tree/0345bd76123f78b0c6ce525a28db8cb7d4e271dd
[megatron]: https://github.com/NVIDIA/Megatron-LM/tree/3e2807ce5e787f81b7275be2383092fe0c3bfc71
[kvmem-transaction]: https://github.com/kvmem/kvmem/blob/0af10ece3d78edb905c62357e52a62147df69d58/kvmem/src/host/memory_contract.cpp#L213-L337
[kvmem-lrfu]: https://github.com/kvmem/kvmem/blob/0af10ece3d78edb905c62357e52a62147df69d58/kvmem/include/kvmem/lrfu.hpp#L43-L99
[ninfer-mtp]: https://github.com/Neroued/ninfer/blob/81c8ce093b2c1646a87566a8e59d807fcf0ec95c/src/models/qwen3_5/program/speculative/mtp.cpp#L70-L201
[ninfer-proposal]: https://github.com/Neroued/ninfer/blob/81c8ce093b2c1646a87566a8e59d807fcf0ec95c/tools/convert/proposal.py
[strata-suffix]: https://github.com/Niko1221/Strata/blob/296b59d2f73c8f04a805df29c2fa97655adace78/src/spec/suffix_drafter.cpp#L51-L95
[strata-session]: https://github.com/Niko1221/Strata/blob/296b59d2f73c8f04a805df29c2fa97655adace78/src/core/conversation_state.cpp#L270-L379
[strata-kv]: https://github.com/Niko1221/Strata/blob/296b59d2f73c8f04a805df29c2fa97655adace78/src/kernels/cuda/kv_stream.cu#L89-L175
[actkv-lrfu]: https://github.com/melis2023/kvmem-actkv/blob/fe4c5d9d57f6714f2ec1d6657d237813d7ec1a89/kvmem/src/host/kvmem_store.cpp#L254-L302
[actkv-action]: https://github.com/Maurya30/actkv-open/blob/0345bd76123f78b0c6ce525a28db8cb7d4e271dd/actkv/eviction.py#L152-L221
[actkv-session]: https://github.com/Maurya30/actkv-open/blob/0345bd76123f78b0c6ce525a28db8cb7d4e271dd/actkv/hf/session.py#L191-L200
[megatron-router]: https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/megatron/core/transformer/moe/router_replay.py#L195-L240
[megatron-metadata]: https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/megatron/core/inference/contexts/dynamic_context.py#L2793-L2819
[megatron-test]: https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/tests/unit_tests/inference/test_moe_permute.py#L562-L604

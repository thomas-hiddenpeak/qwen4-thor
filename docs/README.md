# docs/ — 开发记录文档体系

本目录是项目的开发记录, 目标是:**任意 agent 在任意时间点进入项目,
读完这里就能准确了解当前进展、设计决策和下一步**。

| 文档 | 职责 | 更新时机 |
|---|---|---|
| [OFFLOAD_DECODE_LOG_2026-10-05.md](OFFLOAD_DECODE_LOG_2026-10-05.md) | 显式 decode 日志单因素候选、A/B/C 对照与冻结验收 | Goal进行中；实现与静态复核，尚无新测试结论 |
| [OFFLOAD_REQUEST_POLICY_2026-10-04.md](OFFLOAD_REQUEST_POLICY_2026-10-04.md) | 完整请求长度分区、混合历史与六档 HTTP、独立资源证据 | 有效 NO_GO；长档 TTFT 改善，短档/decode 门槛未过；默认关闭 |
| [OFFLOAD_AUTONOMOUS_2026-10-03.md](OFFLOAD_AUTONOMOUS_2026-10-03.md) | 十小时自主队列、在线候选与冻结HTTP决定门槛 | Goal进行中，当前尚无新验收结论 |
| [OFFLOAD_REPLAY_2026-10-02.md](OFFLOAD_REPLAY_2026-10-02.md) | 实际分块/驻留离线回放与候选收益边界 | Goal完成：重排减少约6%–7% GPU补载，未接受性能 |
| [OFFLOAD_RAM_PROTOCOL_2026-10-02.md](OFFLOAD_RAM_PROTOCOL_2026-10-02.md) | RAM预算修复、缓存/实际IO协议与有界对照 | Goal完成；完整物理RAM及受限cg IO归属仍未闭合 |
| [MOE_OFFLOAD_REPAIR_2026-10-02.md](MOE_OFFLOAD_REPAIR_2026-10-02.md) | offload 基础修复 Goal 范围、身份与验证出口 | 修复与有界验证完成；总物理峰值未知 |
| [MOE_OFFLOAD_DIAGNOSIS_2026-10-02.md](MOE_OFFLOAD_DIAGNOSIS_2026-10-02.md) | offload 分支、代码缺陷、内存/验收/离线方法审查及推进建议 | 诊断快照；后续修复见修复阶段 |
| [GDN_VECTOR_LOAD_2026-09-21.md](GDN_VECTOR_LOAD_2026-09-21.md) | GDN 连续读取、完整 E2E 与状态等价证据 | 本轮验证更新 |
| [2026-09-21 状态历史](HISTORY_STATUS_2026-09-21.md) | 完整保留 fcb5925 时的阶段记录，当前状态已精简 | 历史快照 |
| [GRFrame read/write](GRFRAME_READWRITE_2026-09-21.md) | 提前 inject、frame 所有权与阶段验收 | 验收进行中 |
| [GRFRAME_NORMED_REUSE_2026-09-21.md](GRFRAME_NORMED_REUSE_2026-09-21.md) | normed 区域复用候选、容量边界与 E2E 门禁 |
| [DECODER_WORKSPACE_LAYOUT_2026-09-21.md](DECODER_WORKSPACE_LAYOUT_2026-09-21.md) | decoder 容量与偏移单源化候选及布局对照 |
| [NORMED_MOE_ALIAS_2026-09-21.md](NORMED_MOE_ALIAS_2026-09-21.md) | normed 借用 MoE 区域的别名、生命周期与容量验收 |
| [GRREAD_SCRATCH_2026-09-21.md](GRREAD_SCRATCH_2026-09-21.md) | GRRead down/up 接受，分配各 554→362/步，性能持平 |
| [GRFRAME_GATE_2026-09-21.md](GRFRAME_GATE_2026-09-21.md) | gate 工作区已接受，分配各 362→266/步，新增 64 KiB |
| [DECODER_RESOURCE_CONTRACT_2026-09-21.md](DECODER_RESOURCE_CONTRACT_2026-09-21.md) | 资源合同已接受，布局与错误合同检查通过，性能持平 |
| [PREFILL_CHUNK_SEQUENCE_2026-09-21.md](PREFILL_CHUNK_SEQUENCE_2026-09-21.md) | 文本分块 prefill 序列接口候选 | 状态边界演进时 |
| [SERVE_READBACK_COMMIT_2026-09-21.md](SERVE_READBACK_COMMIT_2026-09-21.md) | serve GPU 结果发布边界候选与验收范围 | 跟踪结果提交时 |
| [LINEAR_SCRATCH_OWNER_2026-09-21.md](LINEAR_SCRATCH_OWNER_2026-09-21.md) | 线性 scratch 所有权复核通过，时间线运行中 |
| [GRREAD_PAIR_MIX_2026-09-21.md](GRREAD_PAIR_MIX_2026-09-21.md) | GRRead 成对读取已接受，TTFT 改善、decode 持平 |
| [GRWRITE_READ_FUSION_2026-09-21.md](GRWRITE_READ_FUSION_2026-09-21.md) | 首版未接受；向量读取第二版完整验收通过，TTFT 改善 |
| [短路径 top-k](SHORT_TOPK_2026-09-21.md) | 精确选择、完整 E2E 与局部时间线 | 本轮验收完成 |
| [短上下文索引打分](INDEXER_DECODE_2026-09-21.md) | CTA 映射、逐位分数与配对 HTTP | 本轮验收完成 |
| [Decode 多级 top-k](DECODE_TOPK_2026-09-21.md) | 寄存器网络、完整 E2E 与逐位证据 | 本轮验收完成 |
| [QSA 输出列分片](QSA_DECODE_SPLIT_2026-09-21.md) | 单 token 四分输出、完整 E2E 与逐位证据 | 本轮验收完成 |
| [MoE 设备执行](MOE_DEVICE_DECODE_2026-09-21.md) | 单 token GPU 专家执行、五档 E2E 与同步计数 | 本轮验收完成 |
| [TOPK_REGISTER_NETWORK_2026-09-21.md](TOPK_REGISTER_NETWORK_2026-09-21.md) | 流式 top-k 网络寄存器化候选与验收 | 本轮验证更新 |
| [HC_GATE_FUSION_2026-09-21.md](HC_GATE_FUSION_2026-09-21.md) | HC 投影与 gate 融合候选和验收 | 本轮验证更新 |
| [GEMV_STAGING_2026-09-21.md](GEMV_STAGING_2026-09-21.md) | BF16 GEMV 输入搬运候选与验收 | 本轮验证更新 |
| [HC_INJECT_GEMV_2026-09-21.md](HC_INJECT_GEMV_2026-09-21.md) | HC 四输出 decode 投影形状分析与验收 | 本轮验证更新 |
| [LINEAR_WORKSPACE_2026-09-21.md](LINEAR_WORKSPACE_2026-09-21.md) | 线性注意力临时缓冲生命周期及合并分配验收 | 本轮验证更新 |
| [MOE_ROUTING_ALLOC_2026-09-21.md](MOE_ROUTING_ALLOC_2026-09-21.md) | MoE 分配隔离实验与合并分配验收 | 本轮验证更新 |
| [MOE_WORKSPACE_2026-09-21.md](MOE_WORKSPACE_2026-09-21.md) | MoE 路由元数据生命周期与 E2E 验收 | 本轮验证更新 |
| [SPARSE_LAYOUT_2026-09-21.md](SPARSE_LAYOUT_2026-09-21.md) | 稀疏注意力 shared 行布局、E2E 与逐位数值验证 | 本轮验证更新 |
| [TOPK_WARP_2026-09-20.md](TOPK_WARP_2026-09-20.md) | 流式 top-k warp 交换、失败候选与 E2E 验收 | 本轮验证更新 |
| [OUTPUT_LIMIT_2026-09-20.md](OUTPUT_LIMIT_2026-09-20.md) | 输出上限后的无用前向修复、边界与五档 E2E | 本轮验证更新 |
| [HTTP_TIMELINE_2026-09-20.md](HTTP_TIMELINE_2026-09-20.md) | 已验收版本的五档 HTTP 时间线、阶段边界与优化优先级 | 新诊断更新 |
| [POSITION_METADATA_2026-09-20.md](POSITION_METADATA_2026-09-20.md) | 主模型位置回读消除与 E2E 验收 | 本轮验证更新 |
| [INDEXER_CLEANUP_2026-09-20.md](INDEXER_CLEANUP_2026-09-20.md) | 索引键无用计算清理与等价性/性能验收 | 本轮验证更新 |
| [ACCURACY_AUDIT_2026-09-20.md](ACCURACY_AUDIT_2026-09-20.md) | QSA 数学错误定位、分步质量 E2E 与性能验收 | 本轮验证更新 |
| [BASELINE_2026-09-20.md](BASELINE_2026-09-20.md) | 五档普通 decode 初始 E2E 结果、证据与限制 | 新测量单独成文，不覆盖历史结果 |
| [REPRODUCIBILITY_2026-09-20.md](REPRODUCIBILITY_2026-09-20.md) | 稀疏注意力输出非确定性定位与 E2E 证据 | 后续验证更新 |
| [DISCONNECT_FIX_2026-09-20.md](DISCONNECT_FIX_2026-09-20.md) | SIGPIPE 修复、断连 E2E 与输出复现阻塞 | 新验证追加记录 |
| [EVALUATION.md](EVALUATION.md) | E2E 优先规则、五档矩阵、证据与接受条件 | 用户规则或评估口径变化时 |
| [STATUS.md](STATUS.md) | 当前状态快照: 当前焦点、进行中、卡在哪、下一步 | 每次有意义的改动后 |
| [DONE.md](DONE.md) | 已完成 / 已解决归档 (从 STATUS 分离) | 条目完成时追加 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 整体架构设计: 模块划分、数据流、关键设计决策 | 架构变化时 |
| [PHASES.md](PHASES.md) | 分阶段计划: 每阶段范围、完成标准 | 阶段推进时 |
| [log/](log/README.md) | 开发日志 (按日期分文件, 时间倒序, 不可变) | 每次有意义的改动后 (追加到当天文件, 不改历史) |
| [MODEL.md](MODEL.md) | 目标模型架构笔记: qwen4_exp 各组件的理解 | 理解深入时 |
| [REFERENCE.md](REFERENCE.md) | 参考项目说明: 每个项目取什么、怎么用 | 参考策略变化时 |
| [REFERENCE_MTP.md](REFERENCE_MTP.md) | MTP / 投机解码参考调研 | MTP 相关时 |
| [MTP_BATCHING.md](MTP_BATCHING.md) | MTP 批处理设计与阶段 | MTP 批处理推进时 |
| [DATAFLOW_OPTIMIZATION.md](DATAFLOW_OPTIMIZATION.md) | 当前数据流预算、假设与决策边界 | 数据流优化推进时 |
| [MOE_DISTRIBUTION_2026-09-28.md](MOE_DISTRIBUTION_2026-09-28.md) | 历史轨迹专家ID分布、阶段/层/场景差异和稳定性 | 统计完成，与缓存命中分开 |
| [MOE_SHADOW_2026-09-28.md](MOE_SHADOW_2026-09-28.md) | 有界请求完成边界影子观察、完整长度验证与资源成本 | 功能通过，严格持平未通过 |
| [MOE_CACHE_REPLAY_2026-09-28.md](MOE_CACHE_REPLAY_2026-09-28.md) | 固定容量静态/LRU回放、预算与影子阶段决策 | 本轮离线交付 |
| [MOE_LAYER_TOPN_2026-09-28.md](MOE_LAYER_TOPN_2026-09-28.md) | 每层Top-N完整曲线与覆盖/整组目标的最小名单 | 同样本描述性，非运行时最优 |
| [MOE_HYBRID_2026-09-28.md](MOE_HYBRID_2026-09-28.md) | 固定热点与动态LRU混合容量离线比较 | 离线探索，不认定统一最优比例 |
| [MOE_EVALSCOPE_SCENARIOS_2026-09-28.md](MOE_EVALSCOPE_SCENARIOS_2026-09-28.md) | 24条多场景采样、32/64槽影子统计与128/256离线容量 | 采样完成，非生产代表性 |
| [MOE_ROUTING_TRACE.md](MOE_ROUTING_TRACE.md) | MoE受控采集边界、首轮证据与下一阶段离线缓存回放 | 设计或实现进展变化时 |
| [MOE_RESIDENCY_PLAN_2026-09-30.md](MOE_RESIDENCY_PLAN_2026-09-30.md) | 分层专家驻留与按需加载实现计划、冻结轮与用户决策记录 | 实现中，矩阵执行 |
| [MOE_RESIDENCY_ACCEPTANCE_2026-09-30.md](MOE_RESIDENCY_ACCEPTANCE_2026-09-30.md) | 分层驻留验收报告（第一轮矩阵实测已填） | DRAFT，第一轮完成，第二轮待跑 |
| [MOE_RESIDENCY_L2_PREDICTION_2026-10-01.md](MOE_RESIDENCY_L2_PREDICTION_2026-10-01.md) | L2 命中率离线预测与 C 容量扫描（第二轮预判/第三轮决策输入） | 已完成（离线分析） |
| [MOE_RESIDENCY_MEMORY_LEDGER_2026-10-01.md](MOE_RESIDENCY_MEMORY_LEDGER_2026-10-01.md) | 262144容量完整内存账（预算侧+实测侧） | 已建立，第二轮峰值待实测 |

## 现行入口（2026-09-28）

先读 [STATUS.md](STATUS.md) 与 [EVALUATION.md](EVALUATION.md)。
当前已交付基线为[首版私有文本版本](RELEASE_TEXT_V1_2026-09-28.md)，
阶段切换复核见[基线交接](BASELINE_HANDOFF_2026-09-28.md)。
[收敛清单](CONVERGENCE_2026-09-28.md)已完成；上表专题中的
“候选”“进行中”是当时记录，不能作为当前待办或发布结论。

性能优化第一项测试仍为evalscope HTTP E2E；Bug修复可先做直接
回归。不使用bench。先冻结范围、成组修改、统一验收，只有具体
失败才做定位/消融；未改动且身份一致的证据复用。

[历史状态快照](HISTORY_STATUS_2026-09-20.md) 与
[历史数据流分析](HISTORY_DATAFLOW_2026-09-20.md) 保存旧内容，不作为现行结论。
[历史 AGENTS 状态段](HISTORY_AGENT_ENTRY_2026-09-20.md) 也已归档，入口不重复维护状态。
日志原文保持不变，后续更正以新条目说明。

## 写作原则

- **记录事实与决策, 不写空话**。每条记录应能让未参与的 agent 据此
  继续工作。
- **决策要写理由**。"做了什么" + "为什么" + "排除了什么替代方案"。
- **代码是最高事实来源**。文档描述的是"设计意图", 实现细节以代码为准。
- **日志只追加**。开发日志按日期分文件于 [log/](log/README.md); 历史条目
  不修改, 发现错误时追加更正条目; 新条目追加到当天 `log/<日期>.md` 顶部
  并在 log 索引登记。
- **STATUS.md 是快照**。始终反映"现在"; 已完成条目移入 [DONE.md](DONE.md),
  历史时间线由 [log/](log/README.md) 承载。

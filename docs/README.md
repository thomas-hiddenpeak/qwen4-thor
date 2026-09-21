# docs/ — 开发记录文档体系

本目录是项目的开发记录, 目标是:**任意 agent 在任意时间点进入项目,
读完这里就能准确了解当前进展、设计决策和下一步**。

| 文档 | 职责 | 更新时机 |
|---|---|---|
| [GDN_VECTOR_LOAD_2026-09-21.md](GDN_VECTOR_LOAD_2026-09-21.md) | GDN 连续读取、完整 E2E 与状态等价证据 | 本轮验证更新 |
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

## 现行入口（2026-09-20）

先读 [STATUS.md](STATUS.md) 与 [EVALUATION.md](EVALUATION.md)。本轮先治理文档，
再默认关闭 MTP，以 evalscope 建立五档单流基线。E2E 是改动后的第一项测试，
通过后才细分析，不运行任何前置测试或 bench。

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

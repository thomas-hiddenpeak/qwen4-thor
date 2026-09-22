# 开发日志索引 (docs/log/)

> 按时间倒序, **只追加不修改**。每条: 日期、做了什么、为什么、下一步。
> 发现历史错误时追加更正条目, 不改原文。**新增日志追加到当天的
> `docs/log/<日期>.md` 顶部** (当天文件不存在则新建并在此登记)。

| 日期 | 摘要 (当天最新一条) |
|---|---|
| [2026-09-22](2026-09-22.md) | 收拢 STATUS 当前证据入口，8K/44K 三侧复核运行中 |
| [2026-09-21](2026-09-21.md) | 舍入候选前六请求一致，安排无插桩严格质量 HTTP |
| [2026-09-20](2026-09-20.md) | 稀疏注意力 shared 行距 padding 候选，开始真实 HTTP E2E |
| [2026-09-19](2026-09-19.md) | chunked MTP: 长上下文投机解码 (44K 17.7→33.1 tok/s, bit-exact) |
| [2026-09-18](2026-09-18.md) | decode 全面推进: HC mix FP8 (最大遗漏) + 批处理 decode M≤4 (手写胜 cuBLASLt) + 审计 + max_seq 修复 |
| [2026-09-17](2026-09-17.md) | GatedDeltaNet 寄存器-state prefill kernel (ds4 风格, +12% 单流 prefill) |
| [2026-09-16](2026-09-16.md) | prefill roofline: 算力仅用 ~1-4%, 瓶颈是访存+延迟 (非算力); 大乘数在批处理 |
| [2026-09-15](2026-09-15.md) | Step 4: tensor core (FA4) 路径关闭 (源码级 blocker) |
| [2026-09-14](2026-09-14.md) | 262K 内存预算评估 + PD 决定 + PHASES.md 更新 |
| [2026-09-13](2026-09-13.md) | MTP 批处理 Stage 2c: 调度器 MTP 分支 (闭合, 5 增量) |
| [2026-09-12](2026-09-12.md) | 验证标准体系 (Phase 2 完成标准, 已闭合) |
| [2026-09-11](2026-09-11.md) | 视频输入 (Phase 2, 2/3): C++ 视频 processor + 差分验证 (6 case 全过) |
| [2026-09-10](2026-09-10.md) | kernel launch 削减 (续 3): PLE trunk_add 融合 + 删 PleAddTrunkKernel (性能中性, 复杂度下降) |
| [2026-09-09](2026-09-09.md) | MTP 批处理验证 (ModelDecodeBatch): 12.1 → 14.7 tok/s (追平 plain) |
| [2026-09-08](2026-09-08.md) | MTP 接入 generate (--mtp) + 接受率诊断 (负结果: draft 输出垃圾) |
| [2026-09-07](2026-09-07.md) | decode 流量根因分析 + RouterTopk 并行化 (12.2→12.9 tok/s) |
| [2026-09-06](2026-09-06.md) | MTP 接口预留 (scheme A: 主模型暴露 pre-final-mixer 多流) |
| [2026-09-05](2026-09-05.md) | PD-ready 阶段边界 API (ModelSequence) 实现 |
| [2026-09-04](2026-09-04.md) | 模型层启动 (1/N): Hyper-Connection (GatedResidual) 主干 |
| [2026-09-03](2026-09-03.md) | IO 层: 权重加载编排 (WeightIndex + WeightLoader) |

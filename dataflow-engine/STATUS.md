# QTDE 筹备状态

更新：2026-09-20。只描述本目录的设计工作，不替代现有 runner 状态。

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

- 所有新执行计划均未实现，未声称任何端到端加速。
- 硬件实际资源、选定 chunk/tile/CTA 数、graph 映射、精度阈值待定。
- 更新日志、FP8 residual、draft 索引复用是实验，不是默认承诺。
- 不把所有阶段的“权重只读一次”或片上全驻留作为设计假设。

## 下一步

先审阅 [EXECUTION_PLANS](EXECUTION_PLANS.md) 的状态边界和
[RESOURCE_BUDGET](RESOURCE_BUDGET.md) 的范围，再进入 R0/R1：
可机读计划清单与 GRFrame 最小执行闭环。当前没有开始实现代码。

# 真实文本主干MoE捕获与重放结果（2026-10-19）

状态：DIAGNOSTIC_CONTRACT_PASS。既定工具目标完成；这不是整模型质量、
性能接受或上游差异根因定位。无新增生产代码/默认入口变更。

## 范围与证据

[冻结计划](MOE_REPLAY_REAL_INPUT_PLAN_2026-10-19.md)执行一次，无重试。
真实英文账本文本由模型tokenizer编码为76 token：72 token prefill，
位置72～75的4 token作为teacher-forced后缀；只捕获主干层2。
保存完整UTF-8/IDs及逐记录的完整因果前缀。显式调用上下文核对实际
48个layer_id和成功完成边界，未沿用旧observer的计数推断。

1次加载、7次模型前向、17次层重放、24份记录、16组比较全部完成；
两次独立prefill逐位一致，5组同形状重放的hidden/logits/IDs/weights/
output逐位一致。13项Python host测试（8项报告＋5项bundle）通过，
既有上下文测试按未变代码复用。独立目标零警告构建；首次包装符号
可见性链接失败保留，修复后才执行GPU。

全阶段单lease约172秒，退出COMPLETED且子进程0；未记录竞争/监控失败。
flock与1秒轮询不能排除短于采样间隔或不可见的非合作干扰。
201个模型文件共135243525502字节先全量SHA256，运行后核对文件元数据。
此流程依赖模型只读约定，不提供恶意篡改防护。源码/文本/可执行快照
运行前后核验；独立离线审计复核334个文件摘要并从原始数据复算一致。

- [身份、统计与独立审计](evidence/moe-replay-real-20261019/offline-audit.json)
- [实际冻结命令与预算](evidence/moe-replay-real-20261019/gpu-plan.json)
- [完整分项JSON](evidence/moe-replay-real-20261019/gpu-comparisons.json)
- [可读逐行报告](evidence/moe-replay-real-20261019/gpu-report.md)
- [构建与生产隔离核对](evidence/moe-replay-real-20261019/build-audit.json)

## 观察与解释边界

| 逻辑位置 | 输入hidden不同元素 | IDs slot不同 | 专家集合不同 | MoE输出max abs |
|---|---:|---:|---:|---:|
| 72 | 0 | 0 | 0 | 0 |
| 73 | 0 | 0 | 0 | 0 |
| 74 | 2368 | 2 | 0 | 0.0032958984375 |
| 75 | 2310 | 0 | 0 | 0.00347900390625 |

以上比较真实T1连续前向与独立prefill后的T4。位置74、75在进入层2前
hidden已经不同，因此不能将相应输出差异归因于层2专家算术。
位置74的expert 496/84仅互换slot，选中集合不变。将T4有序IDs用于
实际T1 hidden后，四行的MoE输出均与原T1逐位一致；这份夹具没有
观察到该选择顺序干预改变输出。

将T4各行hidden完全相同的字节用于T1重放，正常路由和固定IDs两类
对照均与T4对应行逐位一致，包括全部logits、选中IDs、当前权重及
MoE输出。由此仅能说本层在这四行相同输入上未出现形状差异，不能
推广到所有文本/层/位置或一般batch不变，也尚未定位上游差异来源。
固定IDs仍按当前logits重新计算权重；没有权重冻结、精度变更或
把剩余差异自动归因于routed experts。shared路径仍在MoE输出内。

## 复现与保留

[工具入口及命令](../tools/verify/moe_replay_real/README.md)。新运行必须
使用新的build/.q4t-work输出目录及独占锁，不覆盖旧证据。
原始完整运行保留在`.q4t-work/moe-replay-real-20261019/gpu-v1`，包含
执行binary、源码快照、模型文件清单、真实层hidden及route.bin；锁
事件在同级`lease-v1`。受版本控制的证据保留摘要、审计与完整报告，
大文件与原始payload保留本机，不能仅凭GitHub报告重新构造原始输入。

无需GPU即可用保存的源码对全部原始记录再次独立校验：

```bash
python3 -B docs/evidence/moe-replay-real-20261019/offline-audit.py
```

诊断binary为bf5c6ea040a1d41064ca6a8ff106f8f823a5cfcd7941b62d3a986bd09e34133c。
构建缓存位于`.q4t-work/moe-replay-a1-20261019/build`，精确执行副本
位于本阶段`gpu-v1/executable`。对应源码digest与模型/权重/tokenizer
身份见审计文件；Git commit与完整源码列表见原始run.json。

## 生产保护与交付边界

新增链接包装仅用于EXCLUDE_FROM_ALL独立目标。src/include无变化，
生产MoE所有SHF_ALLOC段与A1证据一致；诊断程序中实际decoder调用
指向wrapper，wrapper仍调用原MoEForward，且真实48层上下文已闭合。
默认build/q4t SHA仍为44abd8b1，未替换服务入口或启动服务。
HTTP新增0，按未变生产依赖复用A1/B1b质量与五档记录；本次不新增
质量、性能或部署结论。不扩展MTP草稿、offload或CUDA Graph。

下一步如需定位位置74/75的上游来源，应另冻层边界范围；本阶段不
自动追加采样。回退仅撤销独立工具/CMake目标，不涉及模型与默认服务。

# Offload 分支收尾与主线交接（2026-10-07）

本轮从真实 `main` 的 `8ea85b1b0cdf0f026247bf185bddbd536f916c82`
建立独立分支 `codex/main-wrapup-20261007`，只整合以下三个包。
原开发目录仍在 `codex/moe-residency-20260930`，其中七项未提交内容
与原 `build/q4t` 单独保留。收尾不以实验分支整体合并来改变主线运行方式。

## 有界交付与验收范围

| 包 | 来源 | 主线交付 |
|---|---|---|
| 研究工具与结论 | `8602015`、`15feb57`、`8ea3d32`、`a2fdcfd`、`0df355a` | 路由/缓存回放、供给上界、mirror 保留分析、物理 RAM 证据工具和真实响应 ID 合同；历史 runtime 依赖显式指定 |
| 公共 I/O 修复 | 精选 `2b0100c` | 权重读取期间持有 shard 所有权、LRU 驱逐后元数据指针仍有效、诊断读取加锁 |
| 公共预算修复 | 精选 `cb379bd` | 不可行预算在模型加载前拒绝、绑定实际分配参数、MTP 条件计费、PLE 与 KV 页取整修正 |

未引入 MoEResidency、ReadRangev、3b staging、offload 缓存策略、GEMM
映射变化或任何 NO_GO 性能候选。研究用分区算法放在 `tools/trace/research/`，
只供离线工具编译，不进入模型 target。旧计划绑定旧工具摘要，不能直接套用
到移植后的工具；新分析需重新绑定当前工具与明确的历史 runtime 源码身份。

统一验收先冻结以下范围，完成成组修改后一次执行。保留首次失败，只有
具体失败修复后重验受影响项，不扩大为下一轮优化：

1. 公共 host CTest（原合同及移植工具合成合同），响应 ID 与 RAM 工具合同。
2. I/O 生命周期/并发 LRU 直接合同，以及 ASan/UBSan 检查；H2D host stub
   只验证调用/生命周期，不宣称真实设备拷贝。预算边界直接合同。
3. 本机 Thor Release 零警告构建，保持 C++23、CUDA 13.3、SM110a。
4. 实际 1% RAM 预算启动拒绝，确认发生在模型加载之前。
5. 同一新二进制真实 evalscope HTTP 固定质量 11 题，MTP 关闭、单流、
   max-prefill=8192、max-len=208896，与冻结 quality_reference.json 对照。

这是必要正确性修复与工具收尾，不申请提速或性能持平结论；本轮不跑
五档性能矩阵，也不从质量通过推导性能通过。MTP/媒体生成、完整模型
数值 oracle、长稳和整体物理 RAM 54 GB 保证均不在本轮验收范围内。
预算是分配估算，不是实际内存峰值或 OOM 的充分保证。
审查发现真实 workspace sizing 之前还需拒绝超出当前实现上限 8192 的
max-prefill；该入口修复与 0/8192/8193/INT_MAX 边界合同同组验收。
MTP 开启时，独立 BF16 draft 权重、lazy scratch/verify checkpoint 和
并发请求 trunk 重叠并未完整计入；本轮只修正条件计费，不能把 MTP
`feasible=true` 当作完整预算闭合。错误的“trunk 仅一份”峰值说明一并更正。

本地审计根：`.q4t-work/main-wrapup-20261007/`；新构建和测试仅写其
`source/build/`。原始开发状态在 `entry.json` 冻结。测试结果在完成后追加。

## 研究分支的保留价值与停止点

所有 offload 后续分支均汇入同一研究历史，最新封存 tip 为
`7672b591644add0bbc05b8782da3127c80edc04d`，原分支与证据不删除。
这些分支有研究价值，但不等于已有待默认启用的性能提升。

| 研究线 | 保留结论与入口 |
|---|---|
| 分区及按请求策略 | 完整性能筛查 NO_GO；[分区报告](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/7672b591644add0bbc05b8782da3127c80edc04d/docs/OFFLOAD_PARTITION_RUNTIME_2026-10-03.md)、[请求策略](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/7672b591644add0bbc05b8782da3127c80edc04d/docs/OFFLOAD_REQUEST_POLICY_2026-10-04.md) |
| 诊断与历史影响 | 前驱/后继存在可观测差异，单独因果未闭合；[诊断](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/7672b591644add0bbc05b8782da3127c80edc04d/docs/OFFLOAD_DIAGNOSTICS_2026-10-04.md)、[受控历史](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/7672b591644add0bbc05b8782da3127c80edc04d/docs/OFFLOAD_CAUSALITY_2026-10-05.md)、[机制](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/7672b591644add0bbc05b8782da3127c80edc04d/docs/OFFLOAD_MECHANISM_2026-10-06.md) |
| Decode 日志 | 去掉目标日志仍完整性能 NO_GO；[报告](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/7672b591644add0bbc05b8782da3127c80edc04d/docs/OFFLOAD_DECODE_LOG_2026-10-05.md) |
| GPU 源码一致回放 | 固定入口与顺序下复现基线；expert-ID 平票候选增加工作量，未准入 runtime；[报告](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/7672b591644add0bbc05b8782da3127c80edc04d/docs/OFFLOAD_GPU_REPLAY_2026-10-06.md) |
| Decode 供给与 mirror 保留 | 同 plan 竞争存在，但不能解释全部供给差；保留分析可约束下一次假设；[供给报告](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/7672b591644add0bbc05b8782da3127c80edc04d/docs/OFFLOAD_DECODE_SUPPLY_2026-10-06.md)、[保留报告](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/7672b591644add0bbc05b8782da3127c80edc04d/docs/OFFLOAD_MIRROR_RETENTION_2026-10-06.md) |
| GPU-covered mirror 回收 | 17 个服务、131 个 HTTP 的固定范围完成，性能 NO_GO；不继续追加策略；[最终报告](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/7672b591644add0bbc05b8782da3127c80edc04d/docs/OFFLOAD_MIRROR_RECYCLE_2026-10-06.md) |

这些结论只适用于各自报告中的源码、二进制、输入和规则。移植工具的
合成测试不重复证明历史实测结果，也不把离线加载次数当作 HTTP 吞吐。
完整 offload 能力今后若进入主线，需要单独范围、依赖审查和新的验收；
本轮结束后主线可直接推进其他任务，不依赖继续探索 offload。

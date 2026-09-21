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

## 最新接受：GRFrame read/write 第一阶段

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
最新运行时为上述 GRFrame 阶段，性能持平，不重置参考。
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
`.q4t-work/e2e/short-topk-register-20260921/`。build/q4t 对应 GRFrame 已验证二进制，
不是 fcb5925 的原二进制；两版均在 GRFrame 证据目录保存。

## 后续演进

GRFrame 第一阶段已接受。下一步单独合并 normed 非重叠生命周期，
然后再考虑 arena 别名，不把调度顺序、合并和别名混为一次改动。完整 D/P/S、可执行计划和状态提交仍未实现。
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

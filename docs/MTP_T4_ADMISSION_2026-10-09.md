# MTP T4 数值准入阶段协议（2026-10-09）

用户 2026-10-09 指示继续推进 MTP 性能改善。T1（sequential）合同下
的开销归因已完成且 NO_GO（见
[MTP_PERF_ATTRIBUTION_2026-10-09.md](MTP_PERF_ATTRIBUTION_2026-10-09.md)）：
verify 每接受 token 一次 target B=1 前向不可约，成本下界约 3% 慢于
普通 decode。唯一可快于普通 decode 的路径是 T4 批量验证（每步一次
打包 target 前向），10-07 五档实测 decode +18.7%–61.4%（1K/4K/8K/
44K/200K），但 TTFT 各档增加（44K +1.52 s、200K +8.3 s）、200K 整
请求 +1.69%，且 T4 改变 target 验证算术（M=4 GEMM 与 M=1 GEMV 累加
顺序、HC GEMV/GEMM 切换、GDN reduction 次序、短上下文 indexer、
QSA decode/prefill、MoE 分组），存在已知跨形状 logit 差异（如
220/359 同分 5.1875 例，T4 为 5.125/4.96875），无统一误差上界。

本阶段以 T4 数值准入为唯一目标：先界定 T1/T4 分叉，再补生命周期
与长档证据，最后走五档 E2E＋质量 11 题。T4 准入不改变默认行为：
MTP 默认关闭，显式开启仍走 sequential；T4 保持实验状态直至本阶段
出口满足。

## 基线与身份

- 工作树 `.q4t-work/mtp-perf-20261009/source`（分支
  codex/mtp-perf-20261009），binary `cce55e9d`（88fbe4b＋sequential
  路径 Q4T_MTP_TIMING 插桩，默认关闭、不改数值）。T4 路径源码与
  main 7f6a69c 相同（插桩未触及）。
- 固定输入沿用归因组：`attribution-20261009/inputs/ctx1024.jsonl`
  与 `ctx4096.jsonl`（各 3 条，与冻结基线同输入），输出 256、
  greedy、单流、max_prefill=8192、max_len=208896。
- T1 对照数据复用已封存的 sequential-timing-20261009（同 binary、
  同输入），不重跑。

## 阶段顺序

1. **T1/T4 数值分叉界定（本阶段第一项）**：同 binary、同固定输入，
   T4 组（--mtp --mtp-verifier t4）1K/4K 各 3 次；与 T1 组输出做
   token 级对比：逐请求记录首个分叉位置、分叉 token 对、后续是否
   再汇合、整请求同/异。产出：分叉率（请求级与 token 级）、首分叉
   位置分布、已知 220/359 类同分翻转是否复现。只读运行，不改代码。
2. **首分叉数值证据**：对第 1 步发现的首分叉位置，取两路在该位置
   的 logits/argmax 证据（复用现有 dump 工具或最小只读插桩，若需
   插桩须默认关闭且不改数值，并重建 binary 后同 binary 重跑两组）。
   判定：分叉是否全部可归因于已知的算术次序差异（GEMM/GEMV、
   reduction、分组）且无超出有限精度舍入的异常量级。
3. **生命周期缺口补齐**：失败退出排空、stop/输出消费边界、独立
   状态证据（对照 T1 strict 已验收合同，仅补 T4 缺口项）。
4. **五档 E2E＋质量 11 题**：T4 vs 普通（--no-mtp），五档各 3 次、
   输出 256，tools/evalscope 真实 HTTP；固定质量 11 题。TTFT/整
   请求按冻结口径比较，不放宽门槛。
5. **出口判定**：见下。

## 出口条件

- **GO_FOR_T4_ADMISSION**：第 1–2 步分叉全部可解释（或误差有统一
  上界且不改变已验收任务的 argmax 判定）、第 3 步缺口闭合、第 4 步
  五档 decode 不低于普通且质量 11/11、容量与身份保持、服务正常
  退出。进入部署讨论（T4 仍须显式开启）。
- **NO_GO_FOR_T4_ADMISSION**：任一步出现不可解释分叉、无法界定的
  误差、生命周期缺口无法有界修复、或五档/质量失败。记录在案，
  T4 保持实验状态，MTP 性能改善阶段以 T1 成本记录收尾。

## 约束

- GEMM 冻结；不改 T1 数值合同；MTP 默认关闭、显式默认 sequential
  不变；T4 任何改动不得影响 --no-mtp 与 sequential 路径。
- 性能改动第一项测试为真实 HTTP E2E（EVALUATION.md）；第 1–2 步是
  只读诊断，不产生接受结论。
- 证据放 `.q4t-work/mtp-perf-20261009/t4-admission-20261009/`；
  模型/reference 只读；首败保留，不反复采样寻找有利结果。
- 每步更新 STATUS 与当天日志；阶段边界 commit 并推送工作分支。

## 与 T1 阶段的关系

T1 阶段的 NO_GO 结论（冻结目标不可达）不因本阶段改变；本阶段是
用户"推进 MTP 性能改善"指示下的新工作流，目标、合同与出口独立。
T4 若最终 NO_GO，MTP 保持默认关闭，10%–12% T1 开销与 T4 分叉
记录一并作为当前成本与边界存档。

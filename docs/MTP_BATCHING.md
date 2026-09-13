# MTP 批处理 — Stage 2 设计 (待审阅)

> 状态: **设计稿, 未实现**。Stage 1 (draft 状态池化 + 多序列 MtpForward)
> 已闭合 (commit 0b5dfff)。本文是 Stage 2 的精确设计 + 风险点 + 决策点,
> 供审阅后实现。

## 目标

让多个 MTP 请求**并发**享受连续批处理的聚合吞吐。当前 MTP 请求独占
`model_mu_` 整个投机循环 (draft 循环 + 验证 + extend), 不能并发 — 长上下文
损失最大 (MTP 加速比 1.4x 短 → 1.87x 8K)。

## 投机解码一步的三段 (单序列现状)

1. **draft 循环**: 逐 token 生成 d_0..d_{k-1} (每步依赖上一步, 串行链)。
2. **验证前向**: 喂 [b, d_0..d_{k-1}] (k+1 token) 一次主模型 forward,
   惰性比对接受 a 个。
3. **extend**: 用接受前缀重建 draft KV, 产出 next_d0 + next_g。

## Stage 2 三段批量化方案

### 段 1 — 批量化 draft 循环 (低风险, 只动 MTP 代码)

draft 循环每步 j: 每序列从 draft[j-1] 生成 draft[j]。B 序列打包成
**一次 `MtpForward` (d_seq_id, B 个 token, 每序列 1 个)** — 这正是 Stage 1
已支持的多序列 MtpForward。滚动 trunk `d_g_` 改 per-seq
(`[max_seq, hc_dim]`)。

- 风险: 低。复用 Stage 1 已验证的多序列 MtpForward。
- 收益: draft 循环 (k 步 × B 序列) 从 B×k 次 forward 降到 k 次。

### 段 2 — 多序列验证前向 (高风险, 动核心 kernel) ★

每序列喂 (k+1) 个 token, B 序列打包成一次主模型 forward (T = B*(k+1),
d_seq_id)。GEMM 无状态自动打包 (免费)。**难点在因果掩码**:

- **full attention 因果掩码按绝对 position**: 现有 kernel 让 token 只
  attend 到 position 更小的 token。单序列 prefill / B2a 多序列 decode
  (每序列 1 token, 历史在 paged cache) 都不需要"同序列多 token 同 batch
  因果掩码"。但验证前向是每序列 k+1 token 同 batch 因果注意力 — 若两序列
  绝对 position 区间重叠, 按 position 的掩码会**错误允许跨序列注意力**
  (静默污染)。
- **修法**: full attention 因果掩码改 **(seq, position) 感知** — 每 token
  只 attend 到 `d_seq_id` 相同且 position 更小的 token。需要 kernel 知道
  per-seq 的 position 偏移/区间。
- **linear attention (GDN + conv)**: 需要 per-seq 因果链 (prefill 语义) +
  d_seq_id 隔离。B2a 的多序列 decode kernel 是每序列 1 token (无 batch 内
  链), 不能直接用于验证 (需 batch 内 k+1 token 因果链)。需新增 per-seq
  因果 prefill kernel (或让 prefill kernel 支持 d_seq_id)。
- **回滚**: 验证推进 recurrent 状态 k+1 步, 部分接受 a_i 需回滚到 a_i。
  现有单序列用 fused per-token checkpoint (linear_attention.cu 的
  CausalConv1dWithCkptKernel + GDN ckpt)。多序列需 **per-seq per-token
  checkpoint** ([max_seq, num_ckpt, ...] 布局 + kernel 加 d_seq_id)。
  备选 (次优但低风险): per-seq 快照 (ModelSnapshotState 加 seq_id 变体) +
  回滚后重跑 a_i token (重跑不打包, 损失部分收益, 但不动 fused kernel)。

### 段 3 — 调度器 MTP 分支 (中风险, 动 serve)

MTP 步是"多 token 前向" (k+1 验证 token), 与 plain 的"1 token/步"打包
模型不同。调度器需区分 MTP 请求 (走段 2 的验证前向) 与 plain 请求 (走
ModelDecodeBatchMulti)。MTP 请求的 draft 循环 (段 1) 在请求线程内串行,
验证 (段 2) 提交给调度器打包。

## 风险排序与建议

| 段 | 风险 | 触碰 | 建议 |
|---|---|---|---|
| 1 批量化 draft | 低 | 只 MTP 代码 | 可先做, 自包含可验证 |
| 2 多序列验证 | **高** | full attention 因果掩码 + linear prefill kernel + fused checkpoint | **需审阅方案后再动** |
| 3 调度器分支 | 中 | serve 调度器 | 依赖段 2 |

**关键决策点 (段 2)**:
1. full attention 多序列因果掩码: 改 kernel 为 (seq, position) 感知
   (正确, 动核心) vs 限制并发 MTP 请求的 position 区间不重叠 (回避, 但
   限制实用性)。
2. 回滚机制: per-seq fused checkpoint (最优, 动 fused kernel) vs
   per-seq 快照 + 重跑 (次优, 低风险)。

**验证方案** (无论选哪条): 新增 `mtp_verify_multi_seq` 隔离测试 —
B 序列不同 prompt 打包验证, 每序列 vs 单序列参考 (l2_rel 噪声带 + 接受
token 一致), 确认无跨序列污染。沿用 B2a 的判据。

## 建议推进顺序

1. 段 1 (批量化 draft, 低风险) — 可立即做。
2. 段 2 (多序列验证) — **审阅本文决策点后做**。
3. 段 3 (调度器分支) — 段 2 后做。
4. 并发 MTP E2E + 吞吐实测。

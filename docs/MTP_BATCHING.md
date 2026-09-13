# MTP 批处理 — Stage 2 设计

> 状态: **实现中 (2026-09-13)**。Stage 1 (draft 状态池化 + 多序列
> MtpForward) 已闭合 (commit 0b5dfff)。初稿的"full attention 因果掩码
> 风险"经重读 kernel 确认是**误判** (per-seq KV 隔离天然阻止跨序列
> 注意力), 核心工作量在 linear attention 多序列因果 prefill kernel。
> 用户已授权动核心 + 单分支 commit 检查点。

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

### 段 2 — 多序列验证前向 (核心, 动 linear attention kernel) ★

每序列喂 (k+1) 个 token, B 序列打包成一次主模型 forward (T = B*(k+1),
d_seq_id)。GEMM 无状态自动打包 (免费)。

**更正 (2026-09-13, 重读 kernel 后): full attention 因果掩码无需改**。
初稿担心"两序列绝对 position 区间重叠 → 按 position 的因果掩码错误允许
跨序列注意力"。重读 `WriteKVKernel` / `TopkSelectKernel` / `IndexerLogitsKernel`
确认这是**误判**: token t 的 KV 写入 `d_seq_id[t]` 的 per-seq KV 切片 (绝对
position `pos[t]`), 因果掩码 `p <= pos[t]` 读取的也是 `d_seq_id[t]` 的 per-seq
KV 切片 — **per-seq KV 隔离已天然阻止跨序列注意力**, 即使两序列绝对 position
区间重叠也不会污染。full attention 侧 B2a 的多序列 kernel 已支持 d_seq_id,
验证前向可直接复用 (每序列 k+1 token 的 batch 内因果链由 per-seq KV + 绝对
position 掩码自然成立)。

**真正的工作 — linear attention (GDN + conv) 多序列因果链**:
- B2a 的多序列 decode kernel (`GatedDeltaNetDecodeKernel` /
  `CausalConv1dMultiSeqKernel`) 是**每序列 1 token** (无 batch 内链, 历史全在
  per-seq state)。
- 验证前向需**每序列 k+1 token 的 batch 内因果链** (prefill 语义): token t
  attend 到同序列 batch 内 position 更小的 token, 且 recurrent state 沿链递推。
- 需新增 **多序列因果 prefill kernel** (或让现有 prefill kernel 支持 d_seq_id
  选 per-seq state 切片)。这是段 2 的核心工作量。

**回滚**: 验证推进 recurrent 状态 k+1 步, 部分接受 a_i 需回滚到 a_i。
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
| 2 多序列验证 | **中-高** | linear attention 多序列因果 prefill kernel + per-seq checkpoint (full attention 无需改, 见上) | 核心工作量 |
| 3 调度器分支 | 中 | serve 调度器 | 依赖段 2 |

**决策点 (段 2)**:
1. ~~full attention 因果掩码~~ — **已确认无需改** (per-seq KV 隔离天然
   阻止跨序列注意力, 见上更正)。
2. 回滚机制: per-seq fused checkpoint (最优, 动 fused kernel) vs
   per-seq 快照 + 重跑 (次优, 低风险)。用户已授权动核心, 倾向 per-seq
   fused checkpoint (最优), 但可先快照 + 重跑快速闭合再优化。

**验证方案** (无论选哪条): 新增 `mtp_verify_multi_seq` 隔离测试 —
B 序列不同 prompt 打包验证, 每序列 vs 单序列参考 (l2_rel 噪声带 + 接受
token 一致), 确认无跨序列污染。沿用 B2a 的判据。

## 推进顺序 (2026-09-13 更新)

1. **2a 多序列验证引擎** `ModelVerifyMulti`: 每序列 (k+1) token 打包
   一次主模型 forward (d_seq_id), 含 linear attention 多序列因果 prefill
   kernel + per-seq checkpoint (或快照 + 重跑) 回滚。单序列路径 bit 不变。
   测试: `model_verify_multi` 隔离 (B 序列 vs 单序列参考, 接受 token 一致)。
2. **2b 多序列投机步** `MtpSpeculativeStepMulti`: 批量化 draft 循环
   (per-seq 滚动 trunk) + 2a 验证 + per-seq extend。
3. **2c 调度器 MTP 分支**: serve 调度器区分 MTP/plain 请求。
4. 并发 MTP E2E + 吞吐实测。

每步前后 commit 做检查点 (单分支 main, 体现探索过程)。

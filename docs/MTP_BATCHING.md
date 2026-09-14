# MTP 批处理 — Stage 2 设计

> 状态: **Stage 2c 已闭合 (2026-09-13)** — MTP 批处理 Stage 1 + 2a + 2b +
> 2c 全部完成, 并发 MTP 请求已接入 serve 连续批处理调度。
>
> **Stage 2c 实现结果 (2026-09-13)**: `SchedulerLoop` 把 pending 请求
> split 成 mtp_reqs/plain_reqs, MTP 批量跑一次 `MtpSpeculativeStepMulti`
> (B 序列共享投机步), plain 跑 `ModelDecodeBatchMulti`; `HandleChat` MTP
> 投机循环从请求线程直接跑 step 改为注册调度器 + 阻塞 cv, 请求线程推进
> seq (position/history)。`ActiveRequest` 加 `ModelSequence* seq` 指针
> (调度器读其 position/history/seq_id/stage; 请求线程拥有 seq, 运行 step
> 期间请求阻塞 cv 无竞争)。`MtpSpeculativeStepMulti` 签名 `seqs` 改
> `const ModelSequence* const*` (调度器 seq 不连续) + 全部用真实
> `seqs[b].seq_id` (初版用 batch 索引 b 当 seq_id, serve free pool 下
> seq_id≠b 会污染)。verify/extend buffer 持久化 (d_ms_vlogits/vtrunk/
> ext_*)。`MtpDraftExtend` 加 seq_id 尾参。serve MTP 路径 per-seq 化
> (per-seq reset + per-request d_mtp_g + MtpDraftExtend(seq_id))。
> 测试: 68 项全绿零警告 + serve 3 并发 MTP × 3 轮语义正确且确定 (无跨
> 序列污染)。吞吐: 单 200 token ~9.0s, 3 并发 25-30s (聚合 ~22 vs ~21.8
> tok/s, 提升有限) — 根因 MTP 步长错位 (各请求每步接受数不同 → 投机步
> 天然不同步, B 很少达请求数; 实测 B 分布 81×B=1 + 38×B=2), 属 MTP
> 投机解码固有特性。
>
> Stage 1 (draft 状态池化 + 多序列 MtpForward) 已闭合 (commit 0b5dfff)。
> 初稿的"full attention 因果掩码风险"经重读 kernel 确认是**误判** (per-seq
> KV 隔离天然阻止跨序列注意力), 核心工作量在 linear attention 多序列因果
> prefill kernel。用户已授权动核心 + 单分支 commit 检查点。
>
> **Stage 2b 实现结果 (2026-09-13)**: `MtpSpeculativeStepMulti` (B 序列
> 一步投机解码, 三段批量化: 批量化 draft 循环 (per-seq 滚动 trunk
> d_ms_g_pool, k 次 forward 替代 B×k) + ModelVerifyMulti 验证 (Stage 2a,
> per-seq checkpoint 各自回滚) + 批量化 extend (接受前缀连续打包 T=Σ(a_b+1)
> 一次 MtpForward, GatherTrunkRowsKernel 收集 verify trunk 行))。新增持久
> 多序列 scratch (d_ms_*) + 修 MtpModel::Free 既有 d_spec_multi 泄漏。修
> 2 个 bug: ① RunLayers 的 logits==nullptr 仍调 HeadForward → Bf16Gemm
> 输出指针 null 触发 CUBLAS_STATUS_INVALID_VALUE (trunk-only 路径会挂),
> 加 `if(!logits) return` 跳过 lm_head; ② MtpSpeculativeStepMulti 初版给
> ModelVerifyMulti 传 history=nullptr → PLE n-gram 上下文全被 EOS 填充
> (层 1 PLE embedding 污染, logits 全错), 改按 seqs[b].history 构造
> per-seq history。测试 mtp_spec_multi_step (2 层 linear+PLE, B=2 不同
> prompt 长度 4/5, k=3) vs prefill 语义贪心 ground truth: 两序列
> accepted/next_b 全匹配 + next_d0 有效 + 无跨序列污染。68 项测试全绿
> 零警告。
>
> **Stage 2a 实现结果 (2026-09-13)**: `ModelVerifyMulti` (B 序列 × (k+1)
> token 打包一次主模型 forward, sequence-major) + checkpoint 池化布局
> [num_layers, max_seq, cap, elems] + PLE conv checkpoint/回滚 (修复上述
> 正确性发现)。测试 model_verify_multi (B=2 不同 prompt × T=3) vs 单序列
> prefill 参考 6/6 bit-exact + 回滚 0.00896。实现中修 2 个 kernel bug:
> ① DepthwiseConvAddMultiSeqCausalKernel 的 T 参数 grid 边界 (需总数
> B*T) 与局部位置 tt=t%T (需 tokens_per_seq) 混用 → seq≥1 跨序列读
> (加 Tps 参数分离; seq 0 的 t%6==t%3 巧合掩盖了 bug); ②
> PleConvUpdateStateMultiSeqCausalKernel 缺 T<state_len 滑窗 (MTP verify
> T=k+1<9 是常态, 状态窗口不前进)。
>
> **正确性发现 (2026-09-13, 2a 前置, 已修复)**: 单序列 MTP **部分接受**
> (a<k) 时 `ModelRestoreCheckpoint` 只回滚 linear 层 SSM/conv, **不回滚
> PLE 层 `ple_conv_state`** (也是 in-place 递推, 验证后持有含被拒绝
> token 的 gated_n 窗口) → 下一步 PLE short-conv 用到被拒绝 token 的值,
> 系统性 (小) 状态污染; 全接受 (a==k) 无影响。2a 已一并覆盖 (新增
> d_verify_ple_conv_ckpt + Restore 加 PLE 恢复)。

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
   kernel + **per-seq checkpoint (含 PLE conv, 见上正确性发现)** 回滚。
   单序列路径 bit 不变 (PLE 回滚缺口对单序列同时修复)。
   测试: `model_verify_multi` 隔离 (B 序列 vs 单序列参考, 接受 token
   一致) + 部分接受后 PLE conv 状态正确性。
2. **2b 多序列投机步** `MtpSpeculativeStepMulti`: 批量化 draft 循环
   (per-seq 滚动 trunk) + 2a 验证 + per-seq extend。 ✅ 已闭合
3. **2c 调度器 MTP 分支**: serve 调度器区分 MTP/plain 请求。 ✅ 已闭合
   (SchedulerLoop split mtp/plain, MTP 批量跑 MtpSpeculativeStepMulti)
4. 并发 MTP E2E + 吞吐实测。 ✅ 已闭合 (3 并发 MTP × 3 轮正确; 吞吐
   提升有限, 根因 MTP 步长错位, 见上)

每步前后 commit 做检查点 (单分支 main, 体现探索过程)。

## 吞吐实测结论 (2026-09-13, 4b 初版)

3 并发 MTP 请求 (各 120 token) 实测 B 分布 81×B=1 + 38×B=2, 无 B=3。
聚合吞吐 ~22 tok/s vs 单请求 ~21.8 tok/s, 提升有限。根因: MTP 投机步的
步长 = 每步接受数 a_b, 各请求 a_b 不同 (有的 1 有的 3) → 投机步天然
不同步 (ragged), 多数时刻只有 1 个请求在 step。批量化机制本身正确生效
(B=2 的 38 步证明打包发生), 但调度层按"任意 pending 即跑"导致 B 退化。
后续优化见下 (计划 A, 已闭合)。

## 计划 A — MTP 调度 lockstep (2026-09-14, 已闭合)

**根因**: 4b 初版调度器等待谓词是"任意 MTP 请求 pending 即跑", 各请求
接受数不同 → 完成时间不同 → 任意时刻 ready 的 MTP 请求数是 ragged 子集
(1~3), 批量化步的 B 退化 (实测 81×B=1 主导)。

**修复** (对齐 vllm/sglang 的 uniform+lockstep 范式, 见 docs/REFERENCE_MTP.md):
`SchedulerLoop` 的 MTP 等待谓词从"任意 MTP pending"改为"**所有活跃 MTP
请求都 pending** 才跑" (plain decode 仍保持"任意 pending 即跑"的机会式
批处理)。这样批量化 `MtpSpeculativeStepMulti` 的 B 恒等于活跃 MTP 请求
数 (uniform 步宽), 而非 ragged 子集。请求线程侧无需改动 (每步已重新
注册)。最慢请求的 CPU 后处理 (tokenize/SSE) 在 sched_mu_ 外, 不阻塞
调度器。

**实测** (3 并发 MTP, 各 200 token):
- B 分布: 81×B=1+38×B=2+0×B=3 → **5×B=1+16×B=2+37×B=3** (B=3 成主导)。
- 聚合吞吐: ~21.8 → **29.9 tok/s (1.37×)**; wall 25-30s → 20.1s。
- 单请求无回归 (纯 decode ~22 tok/s 一致); 3 并发输出正确, 0 error。
- 残留的 B=1/B=2 步是请求陆续加入/退出 (EOS/max_tokens) 的边界效应, 正常。

**诊断**: `Q4T_SCHED_DEBUG=1` 打印每步 B 值 (env 门控, 默认关)。

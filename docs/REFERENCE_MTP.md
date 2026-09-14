# 参考项目 MTP 批处理 — 调研笔记 (2026-09-14)

> 目的: 为 MTP 批处理后续优化 (计划 A/B/C) 提供 vllm / sglang 的权威参考。
> 参考项目只读, 不参与构建。本笔记记录关键实现位置 + 与我们代码的对照。

## 参考项目版本 (2026-09-14 更新)

| 项目 | 旧 | 新 | 说明 |
|---|---|---|---|
| vllm | 2902ca1 (09-05) | 6711197 (09-14) | shallow, 9 天 1366 文件变化 |
| sglang-ssd-stream | v0.2.0 (08-31) | v0.3.0 (09-10) | **新增完整 spec decoding 实现** |
| sglang-qwen4-exp (单文件) | 0a79825b | 0a79825b (未变) | Thor pin 不变, 无需更新 |

## 计划 A — MTP 调度 uniform + lockstep (修 4b 的 B=1 主导)

**问题**: 我们 4b 实测 B 分布 81×B=1 + 38×B=2, 根因各请求每步接受数不同 →
投机步失步 (ragged), 任意时刻 ready 请求数波动。

**vllm 范式** (`vllm/v1/core/sched/scheduler.py`):
- 每请求每步固定调度 `1 + num_spec_tokens` 个 token (uniform)。
- 不足 pad 到统一尺寸: `padded_num_tokens = 1 + self.num_spec_tokens`
  (line ~979), 注释 "to preserve full cudagraph for this step"。
- 所有请求同步推进, batch 形状恒定 → 批处理天然对齐, B 不退化到 1。
- `assert num_new_tokens == 1 + self.num_spec_tokens` (line ~1194) 强制统一。

**sglang 范式** (`.../speculative/spec_utils.py:98` `resolve_num_tokens_per_req`):
- 单一静态推导点, 每个 phase 的 per-request token 宽度固定:
  - `draft_decode` → `speculative_eagle_topk`
  - `draft_extend` → `speculative_num_draft_tokens`
  - `target_verify` → `spec_algorithm.get_num_tokens_per_req_for_target_verify`
- `EAGLEWorkerV2` (`eagle_worker_v2.py:1197`) 统一调度, `activate_step_by_batch`
  按 batch_size 选 cuda graph。

**结论**: 两家都用 **uniform 投机宽度 + 调度器 lockstep**。我们的算子
`MtpSpeculativeStepMulti` 已能打包 B 序列, 瓶颈在调度层 (每请求 cv 唤醒 →
ragged)。修复方向: 调度器按统一投机步长推进, 接受数小的请求 pad 到统一宽度。

## 计划 B — MTP 复用 QSA top-k 索引 (draft 步不跑 indexer)

**来源**: 技术报告 §2.1.2 "multi-step MTP reuses top-k indices across
prediction steps to further reduce draft-model inference costs"; Tab.4 证明
复用后平均接受长度几乎不变 (4.06→4.07, 无损)。

**sglang 权威实现** (`.../speculative/eagle_worker_v2.py:458`
`_configure_qsa_mtp_index_share`):
- config-gated (`index_share_for_mtp_iteration`), 仅 chain speculation
  (topk==1) 启用。
- `QSAMTPSharedSparseIndices` 池: 存 draft-extend 的 target-aligned 选择,
  按 layer_id 索引, 宽度 = `qsa_token_topk + qsa_compress_ratio - 1`
  (top-k blocks + 未压缩 tail)。
- `set_mtp_shared_sparse_indices(state)` 注入 draft_attn_backend +
  draft_extend_attn_backend。
- 日志: "QSA MTP index sharing enabled: draft decode steps reuse the
  draft-extend selection for layers {layer_ids}"。

**sglang-qwen4-exp 实现** (`qwen4_exp.py:1469` `_compute_qsa_topk_indices`):
- `should_reuse_mtp_sparse_indices(forward_batch)` 为真时 →
  `lookup_mtp_sparse_indices(forward_batch, layer_id)`, **indexer 不跑**。
- 否则跑 indexer + `capture_mtp_sparse_indices` 缓存选择。

**我们现状** (`src/mtp/mtp.cu:1349`): `MtpForward` 每步调
`FullAttentionForward`, 里面 `WriteKV → IndexerLogits → TopkSelect`
(`src/model/full_attention.cu`) 全套重跑。draft 循环 k 步 × 12 full-attn 层
每步重算几乎不变的 top-k 索引 → 可白捡的 draft 加速。

## 计划 C — QSA indexer cache FP8 (报告 §2.2 残差 FP8 的延伸)

**vllm 新变化** (09-05→09-14 diff, `qwen4_exp/nvidia/indexer_qsa.py`):
- `indexer_kv_dtype` 支持 `fp8` (e4m3, 无 scale): Q 和 compressed K 在
  RMSNorm 后量化, logits kernel 直接 fp8×fp8 dot。
- `QSACompressedKeyCache` 从 BF16 改为 `indexer_dtype` (可 fp8)。
- `QSAStateBackend.supported_kv_cache_dtypes` 加 `fp8`/`fp8_e4m3`。

**报告 §2.2**: GR 残差分支用 FP8 存, 相对 BF16 字节减半, 质量几乎无损
(gate 限制了残差值范围)。我们 HC trunk 目前 BF16 (`decoder_layer.cu`
`d_trunk`/`d_res_a`)。

**结论**: QSA indexer cache FP8 是 decode 带宽优化 (indexer 每步读 KV),
GR 残差 FP8 是更宽的残差态带宽优化。两者都 memory-bound 收益, 需验证质量。

## 推进顺序

1. **计划 A** (调度 uniform+lockstep): 直接修 4b 根因, 是 2c 的自然延续。
2. **计划 B** (QSA 索引复用): 独立增量, 白捡 draft 加速, 接受长度无损。
3. **计划 C** (FP8): 带宽优化, 需质量验证, 优先级最低。

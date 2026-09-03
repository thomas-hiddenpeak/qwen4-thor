# MODEL.md — Qwen3.8-Flash-Next (qwen4_exp) 架构笔记

> 对目标模型的理解笔记, 随研读深入持续更新。
> 来源: 模型目录 `config.json` / `ssd-stream.json` /
> sglang-ssd-stream 源码 / transformers 5.8。
> **未确认的语义标注 [待确认]**, 不臆测。

模型目录 (只读):
`~/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream`

## 总览

| 项 | 值 |
|---|---|
| architectures | `Qwen4ExpForConditionalGeneration` |
| model_type | `qwen4_exp` (text: `qwen4_exp_text`) |
| 层数 | 48 (36 linear_attention + 12 full_attention) |
| 层模式 | 每 4 层一个 full_attention (layer 3,7,11,...,47) |
| hidden_size | 2560 |
| vocab_size | 248320 (tie_word_embeddings=false) |
| 最大上下文 | 262144 |
| 多模态 | 图像 + 视频输入, 文本输出 |

## 注意力

### full_attention (12 层)
- GQA: 24 Q heads / 2 KV heads, head_dim 256
- attention_bias=false, dropout 0
- Paged KV cache 目标

### linear_attention (36 层, DeltaNet SSM)
- linear_num_key_heads=16, linear_num_value_heads=48
- linear_key_head_dim=128, linear_value_head_dim=128
- linear_conv_kernel_dim=4
- mamba_ssm_dtype=float32 (SSM state 用 FP32)
- output_gate_type=sigmoid

### 位置编码 (MRoPE)
- rope_theta=1e7, partial_rotary_factor=0.25
- mrope_interleaved=true, mrope_section=[11,11,10]
- (head_dim 256 × 0.25 = 64 维旋转, 3 段 11+11+10=32 对)

## MoE

| 项 | 值 |
|---|---|
| num_experts | 512 |
| num_experts_per_tok (top-k) | 10 |
| moe_intermediate_size | 640 |
| shared_expert_intermediate_size | 640 (1 个 shared expert) |
| router_aux_loss_coef | 0.001 |

## 量化 (NVFP4, ModelOpt 0.46.0)

- quant_algo=NVFP4, W4A4 (权重与激活均 4-bit float, group_size 16)
- **仅路由专家被量化**。`ignore` 列表保持 BF16:
  embed_tokens, mtp.*, self_attn.*, linear_attn.*, mlp.gate*,
  mlp.shared_expert.*, hyper_connection*, ple.*, visual.*, lm_head
- PLE 表: FP8 (float8_e4m3fn)

## PLE (Per-Layer Embedding) — 核心特性

来自 `ssd-stream.json`:

| 项 | 值 |
|---|---|
| 文件 | `ple/qwen3.8-flash-next-ple-fp8.bin` |
| 大小 | 51,200,245,760 字节 (~47.68 GiB) |
| 行数 | 320,001,536 |
| 行宽 | 160 字节 (FP8) |
| dtype | float8_e4m3fn |
| SHA-256 | `b070f9644adf93794d8a1030584ab705809387e64396a9327a68fa3a3a6666b3` |
| 应用层 | `ple_layer_ids=[2]` |
| ple_embed_dim | 2560 |
| ple_conv_kernel_size | 4 |

- 每生成 token 执行 **16 次确定性查找** (README 说法),
  每次取 160 字节行。
- SSD Stream 机制: 行 ID 异步拷到主机固定内存 → 4 KiB 页去重 →
  io_uring 并发读 → 按请求顺序恢复 → 独立 CUDA stream 上
  FP8→BF16 转换 → 仅读取超过重叠窗口时同步。
- 资源: 32 MiB 注册页池 + 2×16 MiB 固定暂存, 工作内存 ~64 MiB。

**[待确认]**
- 16 个 row_id 的计算方式 (与 ngram 相关? `ngram_size=3`,
  `ngram_vocab_size_base=20000000`, `heads_per_ngram=8`,
  `split_ngram_parts=128`, `make_ngram_vocab_size_divisible_by=128`)
- PLE 输出 (16 × 2560?) 如何与主干 hidden 融合
- `ple_conv_kernel_size=4` 的含义 (对查找结果做 conv?)

## MTP (Multi-Token Prediction)

- mtp_num_hidden_layers=1, layer_types=[full_attention]
- hybrid=true
- mtp_use_dedicated_embeddings=false
- mtp.rope_theta=1e7
- MTP 权重保持 BF16 (在 `mtp/` 子目录)

## 其他字段 [待确认]

| 字段 | 值 | 猜测 |
|---|---|---|
| hc_count | 4 | hyper connection 数量? |
| hc_lowrank | 320 | hyper connection 低秩维度? |
| indexer_budget | 2048 | ngram 索引预算? |
| indexer_compress_ratio | 4 | 索引压缩比? |
| indexer_head_dim | 128 | 索引头维度 |
| indexer_kv_heads | 1 | 索引 KV 头数 |
| indexer_n_heads | 4 | 索引 Q 头数 |

## Vision

| 项 | 值 |
|---|---|
| depth | 27 |
| hidden_size | 1152 |
| num_heads | 16 |
| intermediate_size | 4304 |
| patch_size | 16 |
| spatial_merge_size | 2 |
| temporal_patch_size | 2 |
| in_channels | 3 |
| out_hidden_size | 2560 |
| num_position_embeddings | 2304 |
| deepstack_visual_indexes | [] (空) |
| 激活 | gelu_pytorch_tanh |

token id: vision_start=248053, vision_end=248054,
image=248056, video=248057

## 权重文件布局 (观察)

- 每层 4 个专家分片: `layer-NNNNN-experts-0000-0127.safetensors`
  等 (512 专家分 4 片, 每片 ~354 MB)
- 其余权重 (注意力/共享专家/embedding/lm_head/MTP/visual)
  在独立 safetensors 中
- `mtp/` 子目录存放 MTP 权重
- `ple/` 子目录存放 51.2 GB FP8 sidecar (非 safetensors)

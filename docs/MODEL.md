# MODEL.md — Qwen3.8-Flash-Next (qwen4_exp) 架构笔记

> 对目标模型的理解笔记, 随研读深入持续更新。
> 来源: 模型目录 `config.json` / `ssd-stream.json` /
> sglang-ssd-stream 源码 / SGLang `qwen4_exp.py` (权威 forward pass)。
> **未确认的语义标注 [待确认]**, 不臆测。
>
> **权威参考**: `reference/sglang-qwen4-exp/qwen4_exp.py`
> (SGLang @ 0a79825, 即 sglang-ssd-stream 为 aarch64/Thor pin 的版本)。
> 该文件用于对照上游模型语义；本项目实际执行路径以src/代码为准。
> 模型最大配置不是当前发布验收范围，后者见[STATUS](STATUS.md)。

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
- QSA indexer：查询和压缩键均使用 centered RMSNorm（乘 1+weight）；
  压缩键先对原始投影每 4 token 池化，再归一化并在组起点做 RoPE。
  完整组由 top-k 选择，只额外追加未完成组的尾部 token。
  2026-09-20 修复证据见 [精度排障报告](ACCURACY_AUDIT_2026-09-20.md)。
- Paged KV cache (**Phase 1 硬需求**, PD-ready 前提)。✅ 已实现
  (2026-09-05): 按页组织 (`kKvPageSize=16`) + 页表间接寻址
  (`page_table[p] = p/16` 恒等映射下与旧连续布局逐位一致)。

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
- 纯文本 (t=h=w=position) 下 3D 位置退化为一维, MRoPE 等价于标准
  partial RoPE (前 64 维)。旋转公式为 `(a*cos-b*sin, a*sin+b*cos)`。
  2026-09-20 发现并修正旧 kernel 的相反符号；旧 CPU 测试也照用了错误公式，
  因此历史“数学等价已验证”不能作为正确性证据。
- **历史 3D 坐标验证 (2026-09-12)**: 视觉/视频 token 的 RoPE 位置从
  纯文本逻辑位置升级为 transformers 5.16.1 的 3D MRoPE (t, h, w 三行
  坐标 + `mrope_position_delta`)。布局统一为 `[3, max_len]` 绝对位置表,
  prefill/decode/压缩 key 共用一张表; 差分测试 `tools/mrope_diff_test.cpp`
  vs Python 参考 `tools/mrope_ref.py` 63 坐标逐位一致。坐标验证不覆盖
  旋转符号；本轮修正后的多模态 E2E 尚未执行。

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

## PLE (N-gram 哈希查找嵌入) — 核心特性 ★

> **PLE 不是普通的逐层嵌入, 而是 n-gram 哈希查找表。**
> 每个 token 位置根据"前 2 个 token + 当前 token"的 3-gram 上下文,
> 计算 16 个哈希 ID, 从 51.2 GB FP8 表中取 16 行 (每行 160 字节),
> 拼成 2560 维嵌入, 再经投影/门控/卷积后加到主干 hidden 上。

### 表布局 (来自 `ssd-stream.json` + SGLang 源码)

| 项 | 值 |
|---|---|
| 文件 | `ple/qwen3.8-flash-next-ple-fp8.bin` |
| 大小 | 51,200,245,760 字节 (~47.68 GiB) |
| 行数 | 逻辑320,001,446；物理320,001,536（按128行对齐，补90行） |
| 行宽 | 160 字节 (FP8 e4m3) = head_dim_per_ngram (2560/16) |
| dtype | float8_e4m3fn |
| SHA-256 | `b070f9644adf93794d8a1030584ab705809387e64396a9327a68fa3a3a6666b3` |
| 应用层 | `ple_layer_ids=[2]` (**1-indexed**, 即 0-indexed layer 1;
  SGLang `if (layer_id + 1) in config.ple_layer_ids` 证实) |
| ple_embed_dim | 2560 (= 16 heads × 160) |
| ple_conv_kernel_size | 4 |

### 16 个 row_id 的计算（本项目ComputeNgramRowIds，上游_hash_contexts对照）

1. **ngram_heads** = (ngram_size - 1) × heads_per_ngram = 2 × 8 = **16**
   (ngram_size=3, heads_per_ngram=8)
2. **上下文窗口**: 原始token ID按`[t-2, t-1, t]`排列，句首不足
   两个历史token用EOS248044填充，不做词表压缩。历史token与当前token
   之间严格存在EOS时，将该历史token替成EOS；当前token自身是EOS
   不额外清除历史。下式`t[-k]`指此规则处理后的token。
3. **每个 ngram 阶 (2-gram, 3-gram) 各 8 个 head**:
   - 2-gram: `mix = t[0]*m0 XOR t[-1]*m1`
   - 3-gram: `mix = t[0]*m0 XOR t[-1]*m1 XOR t[-2]*m2`
   - 每个 head h: `row_id = mix % vocab_size[h] + offset[h]`
4. **layer_multipliers**: 运行时从checkpoint读取3个奇数乘子。
   `seed + 10007*ple_layer_index`及SplitMix派生仅用于开发交叉核对，
   不替代checkpoint参数。
5. **head词表**: 大小与offset同样从checkpoint读取；本模型各头约20M，
   为从20,000,000起依次取的16个素数，offset累加。逻辑总行数
   320,001,446，物理表按128行补齐到320,001,536。
6. 输出: 每 token **16 个 row_id** (int64), 形状 [tokens, 16]。

> 因此"每 token 16 次查找"= 16 个 head 各查 1 行 (160 字节),
> 共2560个原始FP8字节/token。hash生成row_id范围[0,320001446)，
> 不访问末90个padding行；磁盘实际读取量另受页去重及OS页缓存影响。

### PLE 层forward（本项目PleLayerForward，上游Qwen4ExpPLELayer对照）

输入: `ple_query` = 主干 hidden (layer 2 的 attn 输入, 即
`hidden_states + residual`, 形状 [tokens, hc_count×hidden=10240])。

```
1. embeddings = BF16(BF16(FP8查表(row_ids)) * weight_scale)
                                      # [tokens, 16, 160] → [tokens, 2560]
2. key   = key_proj(embeddings)        # Linear(2560 → 10240)  ReplicatedLinear
3. value = value_proj(embeddings)      # Linear(2560 → 2560)   ReplicatedLinear
4. query = ple_query 重排为 [tokens, 4, 2560]  (hc_count=4 分支)
5. gate  = (norm_key(key) * norm_query(query)).sum(-1) / sqrt(2560)
          # GroupedNorm: 10240 维按 group_size=2560 分 4 组各自 RMSNorm
6. gate  = BF16(sigmoid((gate < 0 ? -1 : 1) * sqrt(abs(gate) + 1e-6)))
7. gated_value = gate * value                    # [tokens, 4, 2560]
8. conv_out = silu( depthwise_conv1d(norm_conv(gated_value)) )
          # Conv1d(channels=10240, kernel=4, dilation=3 (=ngram_size),
          # groups=10240, 零初始化)
          # per-request conv state, 状态长 (4-1)*3=9 列
9. output = gated_value.flatten + conv_out       # [tokens, 10240]
10. hidden_states += output                      # 加到主干 (在 attn_hyper_connection.mix 之前)
```

本项目norm使用Gemma RMSNorm的`1+w`权重；norm输出、gate、
gated_value均有BF16舍入。融合卷积保留`BF16(SiLU(conv))`、
`BF16(gated_value+conv_out)`及`BF16(trunk+output)`的边界；
不能用代数等价的FP32长链直接替换。实现见
[ple_layer.cu](../src/model/ple_layer.cu)。

- **上游 SGLang prefetch 重叠（非本项目当前实现）**: 主模型 forward 循环中, 执行 layer i 之前先调用
  `layer[i+1].ple.start_prefetch()` (若 layer i+1 有 PLE), 在独立
  CUDA stream 上发起 SSD 读取, 与 layer i 的 GPU 计算重叠;
  layer i+1 的 `_prepare_qwen4_exp_attn` 中消费。
- **上游 ngram history 提交**: 每层 forward 后 `_commit_ple_batch` 把
  更新后的 2-token 历史写回 per-request state pool。

### SSD Stream 读取机制 (sglang-ssd-stream backend.py + lib.rs)

以下为上游机制；本项目到层读取、事件及缓冲复用边界见
[ARCHITECTURE](ARCHITECTURE.md)和[本地Gather](../src/ple/ple_embedding.cpp)。

```
GPU: lookup_ids [tokens,16] (int64, GPU)
  → 拷到 pinned host (独立 stream, 非阻塞)
  → 单线程 executor 提交:
      PageReader.gather(ids, rows):
        1. 行 → 4 KiB 页映射, 页去重 (Piece/PageGroup)
        2. io_uring 批量读 (queue_depth=256, max_batch_pages=4096)
           32 MiB mmap 注册页池 (io_uring register_buffers, 失败则回退)
           POSIX_FADV_RANDOM
        3. 按行顺序 scatter 到 pinned staging (2 个 16 MiB slot 轮转)
  → 独立 CUDA stream: Triton kernel FP8→BF16 转换到 GPU output
  → consumer stream wait event (仅此时同步)
```

- 工作内存: 32 MiB 注册页池 + 2×16 MiB pinned staging + 少量 GPU buffer。
- TP 切分: 表按 head 词表范围切到各 TP rank (tp_start/tp_end),
  本实现 TP=1 时全表。

### 权重名映射 (load_weights)

- 常规 checkpoint: `*.ple.ple_embedding.ngram_embedding.shard_N.weight`
  (N 个分片拼成完整表); SSD-Stream 版**已移除**这些分片,
  表在 `ple/*.bin`, 由 SSDStreamEmbedding 接管。
- PLE 层其余权重 (key_proj/value_proj/norm_*/conv1d) 在常规
  safetensors 中, 名字含 `.ple.`。
- 本项目从checkpoint读取layer_multipliers、ngram_heads_vocab_sizes、
  ngram_heads_offsets及BF16标量weight_scale；不在运行时由config重建。
  入口：[LoadPleHashParams](../src/model/model.cu)。

## Hyper-Connection (hc_*) — 已确认

主干不是普通 residual, 而是 **Hyper-Connection** (GatedResidual):

- `hc_count=4`: 每个 token 的 hidden 是 **4 个分支** (4 × 2560 = 10240 维)。
  输入 embedding (2560) 复制 4 份进入主干; 每层内部按 [tokens, 4, 2560]
  处理。
- 每层有 `attn_hyper_connection` 与 `mlp_hyper_connection` 两个
  GatedResidual 模块 (use_mix + use_combine), 做分支的 mix/combine。
- 模型末尾 `hyper_connection_mixer` (GatedResidual, use_combine=False)
  把 4 分支 mix 成最终 2560 维 hidden 给 lm_head。
- `hc_lowrank=320`: GatedResidual 的低秩投影维度。
- `hc_per_branch_norm=true`: 每分支独立 RMSNorm。
- **PLE 的 key/value 投影维度都基于 hc 布局**: key_proj 输出
  10240 (=hc_count×hidden), value_proj 输出 2560, gate 在
  [tokens, 4] 上计算 (见 PLE forward)。

## QSA 稀疏注意力 (indexer_*) — 已确认

full_attention 层 (12 层) 使用 **QSA (Qwen Sparse Attention)**:

- 每层有一个 `indexer` (build_qsa_indexer), 从 hidden_states/positions
  计算 **top-k KV 索引** (`_compute_qsa_topk_indices`), 注意力只在
  选中的 KV 子集上做 (稀疏)。
- `indexer_n_heads=4` / `indexer_kv_heads=1` / `indexer_head_dim=128`:
  indexer 自身的注意力头配置。
- `indexer_budget=2048`: 每 query 选中的 KV 预算 (top-k 上限)。
- `indexer_compress_ratio=4`: KV 压缩比 (indexer 在压缩后的 KV 上选)。
- MTP decode 复用 draft-extend 的 top-k 索引 (不重复跑 indexer)。
- indexer 链可与 qkv 投影链在 alt_stream 上并发 (graph capture 时)。
- 注意力输出有 **gate** (`attn_output_gate=True`):
  `attn_output * sigmoid(gate)` (fused_sigmoid_mul)。

> 这意味着 full_attention 不是标准 dense GQA, 而是带 indexer 的
> 稀疏注意力。实现时需参考 SGLang 的 qsa 模块
> (`sglang/srt/layers/attention/qsa/`)。

## MTP (Multi-Token Prediction)

- mtp_num_hidden_layers=1, layer_types=[full_attention]
- hybrid=true, mtp_use_dedicated_embeddings=false, mtp.rope_theta=1e7
- MTP 权重保持 BF16 (在 `mtp/` 子目录)
- 推测解码 topk=1 (PLE 仅支持 topk=1 的 spec decode)
- MTP decode 复用 target 的 QSA top-k 索引

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

## 完整Forward Pass总览（text，单请求；PLE时序按本项目）

```
input_ids [T]
  → embed_tokens (248320×2560, BF16) → hidden [T, 2560]
  → 复制 4 份 → [T, 10240]  (hc_count=4 分支)
  → host准备原始token ID与per-request 2-token历史

for layer i in 0..47:
    # --- attention 段 ---
    ple_query = hidden (+residual)
    if i == 1:                         # 配置ple_layer_ids={2}是1-indexed
        CPU计算row IDs → host页读取/scatter → H2D/FP8转BF16/scale
        hidden += PLE(embeddings, ple_query)  # 投影+门控+短卷积
    hidden, residual = attn_hyper_connection.mix(hidden)      # 4 分支 mix
    if layer_types[i] == linear_attention:
        hidden = DeltaNet_SSM(hidden)        # 36 层, FP32 SSM state
    else:  # full_attention (12 层)
        topk = indexer(hidden, positions)    # QSA 稀疏索引
        q,k,v,gate = qkv_proj + RoPE(MRoPE)
        hidden = attn(q,k,v; topk) * sigmoid(gate) → o_proj
    # --- MLP 段 ---
    hidden = attn_hyper_connection.combine(hidden, residual)
    hidden, residual = mlp_hyper_connection.mix(hidden)
    hidden = MoE(hidden)   # 512 专家 top-10 + shared, NVFP4 W4A4
    hidden = mlp_hyper_connection.combine(hidden, residual)

hidden [T, 10240]
  → hyper_connection_mixer.mix → [T, 2560]
  → (final norm) → lm_head (2560×248320, BF16) → logits
  → 采样 → next token
```

- linear_attn 层继承 `Qwen3_5GatedDeltaNet` (head-first in_proj,
  权重名 `in_proj_qkvz`/`in_proj_ba` 由 qkv/z/b/a 合并)。
- MoE 专家权重名: `experts.w13_weight` (gate+up 融合) /
  `experts.w2_weight` (down), 512 个专家。
- 视觉: `Qwen4ExpVLModel` 在 text 前插入 27 层 ViT 输出
  (deepstack_visual_indexes 为空 → 单层注入), MRoPE 启用
  (mrope_section 存在且非 language_model_only)。

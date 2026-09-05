# LOG.md — 开发日志

> 按时间倒序, **只追加不修改**。每条: 日期、做了什么、为什么、
> 下一步。发现历史错误时追加更正条目, 不改原文。

---

## 2026-09-05 — 模型层 (7/N): 模型头/尾 (embedding + mixer + lm_head)

**做了什么**
- 新增 `include/q4t/model/model_head.h` + `src/model/model_head.cu`, 并入
  `q4t_model`。实现模型 forward 的头/尾 (层循环之外的部分):
  - `EmbedLookup`: token_ids [T] → emb [T, hs] (行 gather, embed_tokens
    [vocab, hs])。
  - `ExpandTrunk`: emb [T, hs] → trunk [T, hc*hs] (embedding 复制成 hc=4 个
    相同分支, 即 SGLang `cat([emb]*hc)`, 主干残差初值)。
  - `HeadForward`: trunk [T, hc*hs] → `hyper_connection_mixer.mix` (
    use_combine=False 的 GatedResidual, 复用 `HyperConnectionMix`) → [T, hs]
    → lm_head GEMM (`mixed @ lm_head^T`) → logits [T, vocab]。
  - `LoadModelHead` 从 checkpoint 直载 embed_tokens / lm_head (各
    [248320, 2560] BF16) + mixer 三个权重 (hc_norm / mix_down[320,10240] /
    mix_up[10240,320], 无 block_inject)。
- 测试 `model_head_test.cpp`:
  - `model_head_load`: 真实 checkpoint 加载成功 (验证张量名 + shape)。
  - `model_head_forward`: 合成小权重 (vocab 64 / hs 32 / hc 4 / lowrank 8),
    EmbedLookup / ExpandTrunk / HeadForward 与完整 CPU 参考 (gather + 复制 +
    GroupedGemmaRMSNorm + 低秩门控 mix + lm_head GEMM) 对比, logits L2 rel
    3.2e-3。用合成权重避免把 1.27 GB 的 embed/lm_head 读到主机 (GEMM/mix
    数值已由 HC/MoE 测试覆盖)。49 项测试全绿, 零警告。

**下一步**
- MTP 1 层 (mtp_hc: hc_count+1, full_attention) + `mtp.fc_embedding` /
  `mtp.fc_hidden` / `mtp.pre_fc_norm_*` 接线。
- 48 层循环 + 完整模型 forward 编排: EmbedLookup → ExpandTrunk → 48×
  DecoderLayerForward (layer 1 带 PLE) → HeadForward; PLE 的 ple_embeddings
  由 `PleEmbedding::Gather` 对接 SSD stream (ngram 哈希 → io_uring 读 →
  FP8→BF16)。
- 之后: 长序列 QSA 稀疏路径验证 + 拆 `BuildCompressedKKernel` 竞态。

---

## 2026-09-05 — 模型层 (6/N): PLE 注入 decoder layer

**做了什么**
- 把 `PleLayerForward` 接线进 `DecoderLayerForward` (layer 1, 0-indexed,
  checkpoint `ple_layer_ids=[2]` 是 1-indexed):
  - `DecoderLayerForward` 新增 `ple_embeddings` 参数 (device [T, ple_embed_dim]
    BF16, 即 PLE SSD stream 的 ngram gather 结果)。`has_ple` 层在
    `attn_hc.mix` 之前先算 `trunk = hyper_input + ple(ple_embeddings,
    hyper_input)`, 后续 attn_hc.mix / attn_hc.combine 都用校正后的 `trunk`
    (非原始 hyper_input)。非 PLE 层 `trunk` 直接别名 hyper_input, 零开销。
  - `DecoderLayer` 加 `ple` 成员 (PleLayerWeights); `LoadDecoderLayer` 对
    layer 1 自动 `LoadPleLayer`; `Free` 释放。
  - workspace carve 加 PLE 区。新增 `PleLayerWorkspaceBytes(T, hc, hs)`
    (与 `PleLayerForward` 内部 carve 完全一致, 每个 offset 256 字节对齐),
    `DecoderLayerWorkspaceBytes` 加 `has_ple` 参数。
- 测试 `model_decoder_layer_test.cpp` 新增 `decoder_layer_ple_injection`:
  真实 layer-1 (linear + PLE), 两条独立路径从相同零状态出发 —
  (A) 生产 `DecoderLayerForward` (带 ple_embeddings), (B) 手动
  `PleLayerForward` + 元素加法 + 分步子模块 — 输出**逐位一致**
  (A-vs-B L2 rel 0.0)。47 项测试全绿, 零警告。

**踩坑 (两个, 已修)**
- **PLE workspace 算小了**: 初版 `PleWs` 用裸字节和, 但 `PleLayerForward`
  内部 carve 每个 region 边界 `AlignUp(256)`, 实际需要更多 → Route A 报
  "PLE workspace too small"。修复: 加 `PleLayerWorkspaceBytes`, 用与 carve
  完全相同的逻辑 (逐 region AlignUp) 计算, decoder layer 与测试都用它。
- **`PleAddTrunkKernel` 的 `-Wrestrict`**: 原地加法 `o == b` 触发
  "passing argument to restrict-qualified parameter aliases"。修复: 去掉
  该 kernel 三个指针的 `__restrict__` (逐元素读后写, 安全; 非热点)。

**下一步**
- `hyper_connection_mixer` (use_combine=False) 收尾 mix → lm_head。
- MTP 1 层 (mtp_hc: hc_count+1) + 48 层循环 + embedding/norm → 完整模型
  forward (PLE 的 ple_embeddings 由 PleEmbedding::Gather 提供, 待层循环
  对接 SSD stream)。
- 之后: 长序列 QSA 稀疏路径验证 + 拆 `BuildCompressedKKernel` 竞态。

---

## 2026-09-05 — 模型层 (5/N): PLE 层 forward

**做了什么**
- 新增 `include/q4t/model/ple_layer.h` + `src/model/ple_layer.cu`, 并入
  `q4t_model`。实现 PLE 层 (Per-Layer Embedding, 核心差异化特性) 的 forward
  (`PleLayerWeights` + `LoadPleLayer` + `PleLayerForward`):
  1. `key = embeddings @ key_proj^T` [T, hc*hs] (BF16 GEMM)
  2. `value = embeddings @ value_proj^T` [T, hs] (BF16 GEMM)
  3. `key_n = GroupedGemmaRMSNorm(key, norm_key)` (per-branch, group=hs)
  4. `query_n = GroupedGemmaRMSNorm(hyper_input, norm_query)`
  5. `gate[b] = sigmoid(sqrt(|Σ_c key_n·query_n|/√hs)·sign)` [T, hc]
  6. `gated_value[b,c] = gate[b] * value[c]` [T, hc*hs]
  7. `gated_n = GroupedGemmaRMSNorm(gated_value, norm_conv)`
  8. `conv_out = silu(depthwise-causal-conv(gated_n))` (kernel=4,
     dilation=ngram_size=3, 序列前零填充)
  9. `out = gated_value + conv_out` [T, hc*hs]
- PLE 权重全 BF16: `key_proj[10240,2560]` / `value_proj[2560,2560]` /
  `norm_{key,query,conv}[10240]` / `conv1d[10240,1,4]`, 前缀
  `model.language_model.layers.1.ple`。
- 测试 `model_ple_layer_test.cpp`: 真实 layer-1 PLE 权重, 随机 embeddings +
  hyper_input, 与完整 CPU 参考 (投影 + 3×GroupedGemmaRMSNorm + gate +
  depthwise causal conv) 对比, out L2 rel 4.1e-3 (阈值 3e-2)。46 项测试全绿,
  零警告。

**更正**
- 之前条目把 PLE 层写成 "layer 2"。checkpoint `ple_layer_ids = [2]` 是
  **1-indexed** (SGLang `if (layer_id + 1) in config.ple_layer_ids`), 对应
  **0-indexed layer 1**, 权重确实在 `layers.1.ple.*`。PLE 层 forward 在
  0-indexed layer 1 的 `attn_hc.mix` 之前注入。

**下一步**
- 把 `PleLayerForward` 接线进 `DecoderLayerForward` (layer 1, `attn_hc.mix`
  之前, 输出加到 hyper_input)。需 embedding gather 结果作为 PLE 输入
  (PLE SSD stream 的 ngram gather 已就绪, 待与层循环对接)。
- `hyper_connection_mixer` (use_combine=False) 收尾 mix → lm_head。
- MTP 1 层 + 48 层循环 + embedding/norm → 完整模型 forward。

---

## 2026-09-05 — 模型层 (4/N): decoder layer 组装

**做了什么**
- 新增 `include/q4t/model/decoder_layer.h` + `src/model/decoder_layer.cu`,
  并入 `q4t_model`。把已验证的子模块接线成完整 decoder layer
  (`DecoderLayer` + `LoadDecoderLayer` + `DecoderLayerForward`):
  1. `attn_hc.mix(hyper_input)` → mixed_attn [T,hs] + res_a
  2. attn block (linear 或 full, 按 `layer_id % 4 == 3` 选)
  3. `attn_hc.combine(attn_out, hyper_input, res_a)` → combined_a [T,hc*hs]
  4. `mlp_hc.mix(combined_a)` → mixed_mlp [T,hs] + res_m
  5. MoE (routed NVFP4 + shared BF16)
  6. `mlp_hc.combine(mlp_out, combined_a, res_m)` → out [T,hc*hs]
- `DecoderLayer` 持有全部子模块权重 (attn_hc / mlp_hc / linear|full / MoE
  routed+extra) + per-layer 持久 cache (linear: ssm_state [48,128,128] +
  conv_state [10240,3]; full: kv_cache [max_len,2,2,256] + idx_raw/idx_comp
  [max_len,128])。`LoadDecoderLayer` 按 layer_id 自动选 attn 类型并加载 4 组
  权重 (HC 前缀 `attn_hyper_connection`/`mlp_hyper_connection`, attn 前缀
  `linear_attn`/`self_attn`, MoE 前缀 `mlp`)。
- 单一 device workspace 按子模块 carve (attn / moe / moe-gemm / hc), 每个
  offset 256 字节对齐。
- 测试 `tests/model_decoder_layer_test.cpp`: 真实 layer-0 (linear, 无 PLE),
  用两条独立路径从相同零初始状态出发 — (A) 生产 `DecoderLayerForward`,
  (B) 手动分步调用子模块 (独立 workspace 布局) — 输出**逐位一致**
  (A-vs-B L2 rel 0.0)。这验证了层组装的接线 / workspace 划分 / 顺序正确
  (子模块数值已由各自测试保证)。45 项测试全绿, 零警告。

**踩坑 (一个, 已修)**
- **workspace carve 未对齐**: route A 报 `Bf16Gemm failed (status=7)`
  (cuBLAS INVALID_VALUE)。子模块单独测试都通过 (各自 cudaMalloc 独立
  workspace), 区别在层组装用单一 workspace carve — `MoEForwardWorkspaceBytes`
  返回的 `moe_carve` 不是 256 字节对齐, 导致后续 `d_moe_gemm`/`d_hc_ws`
  指针未对齐, cuBLASLt 拒绝。修复: carve 时每个 offset 用 `AlignUp(256)`
  对齐。教训: 从单一 buffer carve 给 cuBLASLt 的 scratch 时, 每个 region
  起点必须对齐 (≥256 字节), 不能裸加字节偏移。

**下一步**
- PLE 层注入 (layer 2, `attn_hc.mix` 之前): SGLang `Qwen4ExpPLELayer.forward`
  (key/value_proj + gated reduce + short_conv)。`PleEmbedding` gather 已就绪,
  待 PLE 层 forward 模块 (short-conv + proj + gate)。
- `hyper_connection_mixer` (use_combine=False) 收尾 mix → lm_head。
- MTP 1 层 + 48 层循环 + embedding/norm → 完整模型 forward。
- 之后: 长序列 QSA 稀疏路径验证 + 拆 `BuildCompressedKKernel` 竞态。

---

## 2026-09-05 — 模型层 (3b/N): full_attention (QSA 稀疏注意力)

**做了什么**
- 新增 `include/q4t/model/full_attention.h` + `src/model/full_attention.cu`,
  并入 `q4t_model` (CMake: `q4t_model` 增加 `full_attention.cu`)。实现
  qwen4_exp 的 full_attention 层 (12 层, 每第 4 层) 的完整 forward
  (`FullAttentionForward`), GQA (24 q / 2 kv head, head_dim 256) + partial
  MRoPE (rotary_dim 64 = 0.25*256, theta 1e7) + attn_output_gate + QSA
  indexer:
  1. 投影: `qg = x @ W_q^T` [T,12288] (Q+Gate 每 head 交错) +
     `k = x @ W_k^T` [T,512] + `v = x @ W_v^T` [T,512] (BF16 `Bf16Gemm`)。
  2. deinterleave qg → q, gate + per-head **centered** RMSNorm(q)
     (`x*rsqrt(mean(x^2)+eps)*(1+w)`)。
  3. centered RMSNorm(k) (in place)。
  4. partial RoPE (前 64 维) 作用在 q, k。
  5. 写 k, v 进 per-layer KV cache (interleaved per position)。
  6. QSA indexer: `iq,ik = x @ W_index_qk^T` → GemmaRMSNorm (plain) +
     partial RoPE → 存 raw ik + 4-token 平均池化 (FP32) → GemmaRMSNorm +
     RoPE → 压缩 K 缓存 → MQA relu logits `sum_h relu(iq·ck)/sqrt(128)` →
     block top-512 → 展开 2048 token 索引 (+ tail)。
  7. 稀疏 GQA 注意力 (online softmax, 按 topk 索引选位置)。
  8. `attn *= sigmoid(gate)`。
  9. `out = attn @ W_o^T` [T,2560]。
- **关键洞察**: 序列 ≤2048 token 时可见压缩 block 数 ≤512 = block_topk,
  QSA 退化为稠密因果注意力 (topk[t]=[0..t]); 稀疏仅在 >2048 生效。indexer
  仍完整运行以匹配参考。
- `LoadFullAttention`: 从 checkpoint 直载 9 个权重
  (`self_attn.{q,k,v,o}_proj.weight` + `q_norm`/`k_norm` +
  `indexer.index_qk_proj.weight` + `indexer.{q,k}_layernorm.weight`)。
- 测试 `tests/model_full_attention_test.cpp`: 真实 layer-3 权重, T=8 (QSA
  稠密退化区), 与完整 CPU 参考 (投影 + centered RMSNorm + partial RoPE +
  稠密因果 GQA + sigmoid gate + o_proj) 一致, **out L2 rel 4.6e-3**。44 项
  测试全绿, 零警告。

**踩坑 (四个, 均已修)**
- **BF16 位转换 (主 bug)**: 初版 `Bf16ToFloat` 用
  `__bfloat162float(__nv_bfloat16(x))`。`__nv_bfloat16(uint16_t)` **没有
  "原始位"构造函数** — u16 被整数提升为 float (如 `0xBF40`=49056 →
  `49056.0f`) 再转 BF16, 彻底破坏位模式。表现: 连"纯拷贝"
  `gate=FloatToBf16(qg[...])` 都错 (值全变), q 输出成 2 的幂。诊断时
  `d_qg` (GEMM 输出) 正确但 `d_gate`/`d_q` 错, 且 memcheck 0 error,
  极难定位。改用 `memcpy` 位操作 (同 linear_attention.cu) 后全对。教训:
  BF16 原始位转换必须走 `memcpy`/`reinterpret_cast`, 不能用 `__nv_bfloat16`
  的值构造。
- **BuildCompressedKKernel group 索引越界**: `(t+1)/compress` 应为
  `t/compress`。t=7 时误算 group=2 → g0=8 → 越界读 `positions[8]`
  (T=8), 污染 CUDA context 致后续 kernel 与诊断拷贝全错。
  compute-sanitizer 精确定位 (block 7, 0xd80000020 out of bounds)。
- **稀疏注意力点积**: 初版误用单标量 `qv` 而非全维
  `sum_j q[j]*K[c][j]`。改为 shared memory 存 q 行 + 全维点积。
- **topk 选择竞态**: 稀疏路径多写共享 `s_sel` 有竞态, 改单线程串行
  (稠密路径本就是 0..pos 填充)。

**下一步**
- 模型层 (4/N): 层组装 — 把 HC mix/combine + linear/full attn + MoE 接线成
  完整 decoder layer (含 per-layer KV/indexer cache 管理 + PLE 层注入)。
- (5/N) `hyper_connection_mixer` (use_combine=False) 收尾 mix → lm_head +
  MTP 1 层。
- 之后: 长序列 (>2048) QSA 稀疏路径端到端验证 + 拆 `BuildCompressedKKernel`
  消除跨 block 竞态。

---

## 2026-09-05 — 模型层 (3a/N): linear_attention (Gated DeltaNet SSM)

**做了什么**
- 新增 `include/q4t/model/linear_attention.h` + `src/model/linear_attention.cu`,
  并入 `q4t_model`。实现 qwen4_exp 的 linear_attention 层 (36 层, 继承
  Qwen3.5 GatedDeltaNet) 的完整 forward (`LinearAttentionForward`):
  1. 投影: `in_proj_qkv` [T,10240] (q|k|v) + `in_proj_z` [T,6144] +
     `in_proj_a`/`in_proj_b` [T,48] (BF16 `Bf16Gemm`)。
  2. causal conv1d (kernel 4, SiLU) 作用在 in_qkv 通道, 持久 conv_state
     [10240, 3]。
  3. Gated DeltaNet 递归 (SSM state [nv=48, kd=128, vd=128], 每 value head
     一个 block, S 放 shared memory, 128 线程)。
  4. 融合 per-head RMSNorm * silu(z) gate。
  5. `out_proj` [T,2560]。
- `LoadLinearAttention`: 从 checkpoint 直载 9 个权重
  (`linear_attn.{in_proj_qkv,in_proj_z,in_proj_a,in_proj_b,conv1d,out_proj,
  norm}.weight` + `A_log` + `dt_bias`)。
- 测试 `tests/model_linear_attention_test.cpp`: 真实 layer-2 权重, T=4,
  零初始 SSM/conv state, 与完整 CPU 参考 (投影 + conv + SSM 递归 + gate +
  out_proj) 一致, **out L2 rel 7.5e-3 / ssm_state 5.0e-3**。43 项测试全绿,
  零警告。

**踩坑 (两个, 均已修)**
- **q/k 归一化误用 RMSNorm**: 参考 kernel 是 L2 风格
  `k_hat = k / sqrt(sum(k^2) + eps)` (**不除 kd**), q 额外乘 `1/sqrt(kd)`。
  初版误写成 `sqrt(mean(x^2)+eps)` (多除了 kd), 误差 ~4e-2。
- **softplus/alpha 指数写错 (主 bug)**: 参考用 `exp2f(x * LOG2E)` (= e^x),
  初版误写成 `expf(x * LOG2E)` (= e^(1.4427x))。因 t=0 时 S=0 使
  `delta=v` 不依赖 alpha, 误差随 token 线性增长 (t=0: 7e-8 → t=3: 3e-2),
  按 token 分解误差后定位。修正为 `expf(x)` 后 GDN 逻辑误差降到 1.2e-5。
- 另: 中间量 (qkv/z/a/beta/y_ssm) 必须**单独 cudaMalloc**, 不能从
  `workspace` carve — 同一 `workspace` 还要传给 cuBLASLt 当内部 scratch,
  会覆盖 GEMM 输出 (与 HC/MoE 的约定一致)。

**下一步**
- 模型层 (3b/N): full_attention (QSA 稀疏注意力) — 需 paged KV cache +
  MRoPE (mrope_section [11,11,10] interleaved) + QSA indexer (compressed
  变体: 4-token 平均池化 → k_layernorm+MRoPE → 压缩 K 缓存 → MQA logits
  block top-512 → 展开 2048 token 索引 → 稀疏注意力 + attn_output_gate)。
  最复杂部分, 需研读 SGLang `qsa/` 的 mqa/topk kernel。
- 之后 (4/N) 层组装 (HC mix/combine + attn + MoE 接线成完整 decoder layer),
  (5/N) hyper_connection_mixer + lm_head + MTP。

---

## 2026-09-05 — 模型层 (2/N): MoE 完整模块 (router + top-k + routed + shared)

**做了什么**
- 新增 `include/q4t/model/moe.h` + `src/model/moe.cu`, 并入 `q4t_model`
  (CMake: `q4t_model` 增加 `moe.cu` 并链接 `q4t_quant`)。
- 实现每层 MLP 的完整 MoE forward (`MoEForward`):
  1. router GEMM `logits = x @ gate^T` [T,512] (BF16, `Bf16Gemm`)
  2. top-k kernel: 按 logit 值选 k=10, **在选中的 k 个 logit 上做 softmax
     归一化** (非全部 E)。对照 qwen35-thor `moe_router_topk_kernel` 确认
     算法 (qwen4_exp 继承 Qwen3.5 的 MoE 结构, 仅 routed experts 改 NVFP4)。
  3. routed NVFP4 experts: 调量化层 `MoERoutedForward` (已实现)。
  4. shared expert (BF16 SwiGLU): gate/up 合并成单 `[2*shared_is, hs]`
     GEMM → SwiGLU → down GEMM。
  5. 门控组合: `out = routed + sigmoid(x @ shared_expert_gate) * shared_down`
     (不加 residual — residual 由外层 Hyper-Connection combine 处理)。
- `LoadMoEExtra`: 从 checkpoint 直载 5 个 BF16 权重
  (`mlp.gate` [512,2560] / `mlp.shared_expert.{gate,up}_proj` [640,2560]
  合并 / `mlp.shared_expert.down_proj` [2560,640] /
  `mlp.shared_expert_gate` [1,2560])。
- 测试 `tests/model_moe_test.cpp`: 真实 layer-2 routed NVFP4 + BF16
  router/shared 权重, T=2 k=10, 与完整 CPU 参考 (top-k + NVFP4 dequant
  routed + BF16 shared + 门控组合) 一致, **L2 rel 1.7e-3**。42 项测试全绿,
  零警告。

**踩坑 (三个, 均已修)**
- **workspace 字节数运算符优先级 bug (段错误根因)**:
  `MoEForwardWorkspaceBytes` 的 return 写成
  `(routed+7) & ~size_t(7) + ((scratch+7) & ~size_t(7))`。`+` 优先级高于
  `&`, 实际解析为 `(routed+7) & (~7 + align8(scratch))`。T=2,k=10 时
  routed=506880, scratch=40608, 正确应返回 `align8(506880)+align8(40608)
  = 547488`, 但 bug 算出 `506887 & 40600 = 38472`。于是 buffer 只分配
  ~38KB, 而 carve 把 scratch 放在 offset 506880 — 全部越界。router GEMM /
  topk 写到越界地址 (恰好落在相邻已映射内存, 不立即 fault), 直到后续
  D2H 读越界地址才段错误。修复: 加括号
  `((routed+7) & ~size_t(7)) + ((scratch+7) & ~size_t(7))`。
- **cuBLASLt split-K 越界写**: `Bf16Gemm` 对 tall-skinny 形状 (M=2, N=512,
  K=2560 的 router GEMM) 选 split-K 算法, 其 `splitKreduce_kernel` 在
  SM110a 越界写 (compute-sanitizer 定位)。修复: 在 heuristic 偏好里设
  `CUBLASLT_MATMUL_PREF_REDUCTION_SCHEME_MASK = CUBLASLT_REDUCTION_SCHEME_NONE`
  (0) 禁用所有 reduction scheme (含 split-K)。split-K 只是性能优化, 非
  split-K 算法始终正确。
- **测试 CPU 参考的未初始化 buffer**: `shared_gu_h` 把单个
  `gate_proj.weight` 张量 (640×2560) 读进 `2*shared_is*hs` 大小的 buffer,
  但 `ReadTensor` 只写张量实际大小, up 半是未初始化垃圾 → shared expert
  up 投影错误 → L2 rel ~1.75 (与 CUDA 端正确拼接的 gate+up 不一致)。这也
  解释了为何 workspace 修复前后 L2 rel 都 ~1.75 (shared 垃圾主导误差)。
  修复: 分别读 gate_proj / up_proj 再拼接, 匹配设备 `shared_gu` 布局。

**为什么重要**
- MoE 是每层都有的核心组件 (512 routed NVFP4 experts + 1 shared BF16
  expert + router)。至此模型层有了 HC 主干 + MoE 两个大块, 只剩 attn
  (full QSA / linear DeltaNet) 与层组装。
- 教训: ① 位运算与算术混用必须加括号 (`&` 优先级低于 `+`); ② cuBLASLt
  的 split-K 在 SM110a 有越界写, 生产代码应禁用 (或验证); ③ 从 checkpoint
  读张量到预分配 buffer 时, buffer 必须与张量实际大小一致, 否则残留垃圾。

**下一步**
- 模型层 (3/N): attn — full_attention (QSA 稀疏注意力, 需研读 SGLang
  `sglang/srt/layers/attention/qsa/` indexer top-k 算法) 与
  linear_attention (DeltaNet SSM, 参考 qwen35-thor deltanet)。
- 模型层 (4/N): 层组装 — 把 attn/MLP 接进 HC mix/combine, 组装完整
  decoder layer (对照 qwen4_exp.py `_prepare_qwen4_exp_attn` /
  `_prepare_qwen4_exp_mlp` / `_postprocess_qwen4_exp_layer`)。
- 模型层 (5/N): `hyper_connection_mixer` 收尾 + lm_head; MTP 1 层。

---

## 2026-09-04 — 模型层启动 (1/N): Hyper-Connection (GatedResidual) 主干

**做了什么**
- 新增 `include/q4t/model/hyperconnection.h` + `src/model/hyperconnection.cu`,
  独立 `q4t_model` 静态库 (CMake 新增 target, 测试链接)。
- **从 SGLang 权威源码核对公式**: `GatedResidual` 不在 `qwen4_exp.py`
  (它 `from sglang.srt.layers.hyperconnection import GatedResidual`), 从
  `python/sglang/srt/layers/hyperconnection.py` (@0a79825) 拉取真实
  `_mix_compute` / `_combine_compute` / `GroupedGemmaRMSNorm`。确认:
  - `mix`: `normed = hc_norm(hyper_input)` (per-branch RMSNorm,
    `hc_per_branch_norm=true` → 10240 维按 4 组各 2560 独立归一, 再乘
    `(1+weight)`); `gate = sigmoid( W_up @ silu( W_down @ normed / hc ) )`;
    `mixed = (gate * normed).view(T,hc,hs).mean(-2)`; 返回
    `(mixed, (hyper_input, normed))`。
  - `combine`: `inject = 2*sigmoid( W_inject @ normed / hc )`;
    `out = ( R.view(T,hc,hs) + block_output.unsqueeze(1) *
    inject.unsqueeze(-1) ).flatten`。
  - `F.linear(x, W) = x @ W^T` (W 行主序 [N,K])。
- **修复关键 bug**: mix 低秩门控是 `silu(x / hc)` (先除后 silu), 初版写成
  `silu(x) / hc`。silu 非线性, 两者差 ~2×, 导致 mix max_rel 2.3。修正
  `SiluDivKernel` 为 `v = x*inv_hc; out = silu(v)`。
- 实现 5 个 kernel: `GroupedRmsNormKernel` (per-branch 块归约, 共享内存
  归约每分支和平方)、`SiluDivKernel`、`MixGateKernel` (gate*normed 跨 4
  分支均值)、`InjectGateKernel`、`CombineKernel`。低秩 GEMM (down/up/
  inject) 复用 `q4t::model::Bf16Gemm` (cuBLASLt, FP32 累加)。
  `LoadHyperConnection` 从 checkpoint 直载 4 权重 (mixer `use_combine=false`
  时无 block_inject)。
- 测试 `tests/model_hyperconnection_test.cpp`: 真实 layer-0
  attn_hyper_connection 权重, 随机 [3, 10240] 输入, mix/combine 与 CPU 参考
  对比。**CPU 参考模拟 BF16 中间量存储** (normed/down/up/inject 都
  `Bf16Round`), 用 **L2 相对误差** (对近零值稳健, 不用 max_rel — combine
  输出含近零值, max_rel 会假性爆到 4.4)。结果 mix L2 rel 3.5e-3 / combine
  2.3e-3 (BF16 精度内)。41 项测试全绿, 零警告。

**为什么重要**
- Hyper-Connection 是 qwen4_exp 主干的残差机制 (非普通 residual): 每 token
  hidden 是 4 分支 × 2560 = 10240 维, 每层 attn/MLP 各一个 GatedResidual
  做 mix (取 2560 给子模块) / combine (子模块输出按 inject 门控注回 4 分支),
  模型末尾 mixer (use_combine=False) 把 4 分支 mix 成 2560 给 lm_head。
  这是模型层的地基, attn/MLP/MoE 都要接进这套 mix/combine。
- 公式细节 (`silu(x/hc)` vs `silu(x)/hc`) 必须对权威源码逐字核对 — 这类
  非线性顺序差异自洽对比抓不到, 只有对照真实 SGLang 实现才暴露。

**下一步**
- 模型层 (2/N): 层内接线 — 把 full_attention (QSA) / linear_attention
  (DeltaNet) 的输出接进 attn_hyper_connection.combine, MoE 接进
  mlp_hyper_connection.combine; 需先研读 qwen4_exp.py 的层 forward
  (1284-1384 行) 与 QSA/DeltaNet 细节。
- 模型层 (3/N): `hyper_connection_mixer` 收尾 (use_combine=False) →
  lm_head。
- 模型层 (4/N): MTP 1 层 (mtp_hc: hc_count+1)。

---

## 2026-09-04 — 量化层收尾 (2/3 + 3/3): grouped MoE GEMM + input_scale 接线

**做了什么**
- **钉死 NVFP4 scale 约定** (真实 gate_proj 权重探针): e4m3 存"放大后"的
  块尺度 `= 块尺度 / scale_2` (值 10~20), dequant = `e2m1 * e4m3 * scale_2`
  (乘)。`e2m1*e4m3*scale_2` → std 0.0127 (合理), `/scale_2` → std 189430
  (荒谬)。
- **修复 act_quant kernel 约定 bug**: 初版 `e4m3 = round(块尺度)` (漏除
  input_scale), 激活重建差 ~1/input_scale (~600×)。`QuantizeActivationToFp4Async`
  加 `global_scale` 参数: `e4m3 = round(块尺度/global_scale)`, e2m1 按
  `e4m3*global_scale` 舍入。CPU 验证 mean|err| 0.79 → 0.075。同步更新
  `quant_fp4_test.cpp` 的 `HostQuantize`/`HostDequant` 与 MoE 测试的内联
  量化。
- 新增 `include/q4t/quant/moe_gemm.h` + `src/quant/moe_gemm.cu` (并入
  `q4t_quant`):
  - `MoERoutedForward(x, expert_ids, router_w, y, weights, ws, gemm_ws, M, k,
    stream)`: routed-expert 完整 forward (512 expert top-k)。按专家分组:
    BuildTokenLists (atomicAdd 计数, token_list 存 flat 索引 t*k+slot) →
    每 expert: GatherQuant (token 行 gather + NVFP4 量化, gu_input_scale,
    写到 a_packed 开头 row 0..M_e-1) → gate/up GEMM (alpha =
    gu_ws2*gu_in) → SwiGLU kernel → 中间激活量化 (dn_input_scale) → down
    GEMM (alpha = dn_ws2*dn_in) → ScatterAdd (router 权重加权累加到 y)。
  - `MoEWorkspace`: 单 buffer 切 6 区 (compact/a_packed/a_sf/gu_out/inter/
    dn_out), `RequiredBytes(M,k,hs,moe_is)` 只依赖 M*k (非 E)。
  - 新增 `QuantizeFloat32ToFp4Kernel` (SwiGLU 中间激活是 float32, 避免
    float→bf16 额外舍入)。
- 测试 `tests/quant_moe_gemm_test.cpp`: 真实 layer-2 权重, M=4 k=3 路由
  (覆盖 M_e=1/2/3 + 共享 expert), 与完整 CPU 参考 (模拟 FP4 量化 + SwiGLU
  + 加权求和, 按真实 dequant) 一致, **max_rel 1.9e-7** (纯 FP32 求和顺序
  差)。40 项测试全绿, 零警告。

**为什么重要**
- 量化层全部完成: 模型层 MoE 的 routed 部分可直接调 `MoERoutedForward`
  (router top-k 选择 + shared expert BF16 在模型层实现)。
- input_scale 接线完成 (3/3): gate/up 输入用 gu_input_scale, down 输入
  (SwiGLU 输出) 用 down_proj 自己的 input_scale, 各 GEMM alpha 折进各自
  (weight_scale_2 * input_scale)。
- 确认 NVFP4 约定 (e4m3 = 块尺度/scale_2, dequant 乘 scale_2) 与
  act_quant 修复后, 整条 routed-expert 数值链路 (真实权重 + 运行时激活
  量化 + SwiGLU + 加权求和) 与 CPU 参考逐位一致。

**踩坑**
- **gather 位置**: expert e 的 token 须写到 a_packed 开头 row 0..M_e-1
  (GEMM 读前 M_e 行), 初版写全局 row e*k+pos 导致 e≥1 的激活错位。
- **router 权重 slot**: 须用 (token,expert) 在 top-k 的真实 slot, 非 token
  在 expert 列表里的 pos (两者不同)。token_list 存 flat 索引 t*k+slot,
  scatter 用 `router_w[flat]` 解决。
- **中间激活 input_scale**: SwiGLU 输出是 down_proj 的输入, 须用
  down_proj 自己的 input_scale (非 gate/up 的)。
- **CPU 参考的 alpha**: GEMM 里 global scale 经 alpha 抵消, 结果 = 真实
  dequant matmul。参考须按真实 dequant (权重×weight_scale_2, 激活×
  input_scale) 且不再乘 alpha, 否则差 ~input_scale 倍。

**下一步**
- 模型层: 48 层 forward (DeltaNet / QSA full-attn / MoE (routed 走
  `MoERoutedForward` + shared expert BF16 + router top-k) / hyper-connection
  / PLE 融合)。

---

## 2026-09-04 — 量化层收尾 (1/3): NVFP4 routed-expert MoE 权重加载编排

**做了什么**
- 新增 `include/q4t/quant/moe_weights.h` + `src/quant/moe_weights.cpp`
  (并入 `q4t_quant`):
  - `MoEWeightLayout`: 每层 4 个大 device buffer — 合并 gate/up packed
    `[2*E*moe_is, hs/2]` 行主序 e2m1 + E 个 per-expert swizzled SF 块、
    down packed `[E*hs, moe_is/2]` + E 个 per-expert swizzled SF 块、
    4 个 per-expert FP32 标量数组 (weight_scale_2 / input_scale, device
    + host 副本)。提供 `gu_packed_expert(e)` / `gu_sf_expert(e)` /
    `dn_packed_expert(e)` / `dn_sf_expert(e)` 切片访问器。
  - `LoadMoEWeights(loader, layer_id, E, hs, moe_is, out, stream)`:
    单次遍历 512 expert, 直载 packed 权重 (跳过 ~73K 次 per-tensor
    cudaMalloc), gate+up 的 weight_scale 合并 (gate 行在前) 后 host 端
    `SwizzleSf`, down 单独 swizzle, 4 个标量 H2D + 存 host 副本。
- 测试 `tests/quant_moe_load_test.cpp` 4 项 (真实 checkpoint, 无 CUDA /
  无模型时跳过):
  - `moe_load_packed_matches_shard`: expert {0,100,511} 的 gate/up/down
    packed 与 shard 逐字节一致。
  - `moe_load_sf_unswizzle_matches_shard`: 反 swizzle 后 SF 与源
    weight_scale 字节一致 (gate/up 合并 + down)。
  - `moe_load_gate_up_share_scale`: 全 512 expert 校验 gate/up 共享
    weight_scale_2 / input_scale, 且 host 副本与 device 一致。
  - `moe_load_gemm_matches_reference`: 加载 expert 0 跑 W4A4 GEMM
    (M=8, N=1280, K=2560), 与 CPU dequant 参考逐元素一致 (max_rel 0.0)。
- 全量 39 项测试通过, 零警告。

**为什么重要**
- MoE 权重 NVFP4 加载打通: 模型层 forward 可直接用 `MoEWeightLayout`
  的切片指针喂 `Fp4Gemm` (W4A4 routed expert)。gate/up 共享 scale 的
  约定 (合并成单 GEMM + 单一 alpha) 在加载期固化, 减少 forward 开销。
- 确认 checkpoint 的 NVFP4 权重 + swizzled scale 直载后, 端到端 GEMM
  与 CPU dequant 参考一致, 数值链路 (shard → packed/swizzled → GEMM)
  完全正确。

**踩坑**
- **合并 gate/up 切片步长 bug**: `gu_packed_expert(e)` 初版用单 proj
  步长 `e * moe_is * hs/2`, 但每 expert 切片含 gate+up 共 `2*moe_is`
  行, 步长应为 `e * moe_is * hs`。错误使 expert e≥1 的 gate 覆盖
  expert e-1 的 up。W4A4 GEMM 测试没抓到 — 它只用 expert 0 (偏移 0)
  且是 device buffer 自洽对比 (CPU 参考 dequant 同一 buffer)。靠
  `moe_load_packed_matches_shard` 对多个非零 expert 与 shard 逐字节
  比对才暴露。教训: 步长/偏移 bug 必须用多个非零索引 + 独立数据源
  比对, 单点 + 自洽对比会掩盖。

**下一步**
- 量化层收尾 (2/3): grouped MoE GEMM 调度 (512 expert top-10 + shared
  expert, 复用 `MoEWeightLayout` + `Fp4Gemm`)。
- 量化层收尾 (3/3): input_scale 在 forward 接线 (激活量化用 per-expert
  input_scale)。

---

## 2026-09-04 — 量化层核心: NVFP4 W4A4 原生路径 + W4A16 dequant

**做了什么**
- 新增独立 `q4t_quant` 静态库 (CMake 链接 `CUDA::cublasLt`):
  - `include/q4t/quant/format.h`: e2m1 / UE4M3 编解码。round-to-nearest-
    even, **无查表** (e2m1 按位分解 + `ldexpf`, e4m3 按位 + `ldexpf`),
    全部 `__host__ __device__` (host-only TU 用 fallback 宏)。UE4M3 与
    e4m3fn 正数位布局一致 (max 0x7E=448, 0x7F=NaN), 与 checkpoint
    F8_E4M3 group scale 一致。
  - `include/q4t/quant/swizzle.h`: NVFP4 scale 张量 swizzle 布局
    (128×64 atom) 偏移公式 + 物理大小 padding 计算 + 行主序→swizzle 转换
    (host 端, 权重加载用)。
  - `src/quant/dequant.cu`: NVFP4→BF16 dequant kernel (W4A16 路径),
    每线程一组 16 值, 含 global scale (`W = fp4 × e4m3 × inv_global`)。
  - `src/quant/act_quant.cu`: BF16→NVFP4 运行时激活量化 kernel (W4A4
    路径), 每线程一组 16 值: gmax/6 → e4m3-rounded scale → e2m1 按
    rounded scale 舍入 (与硬件逐位一致), 输出 packed e2m1 (行主序) +
    swizzled e4m3。
  - `include/q4t/quant/fp4_gemm.h`: cuBLASLt 原生 W4A4 GEMM 封装
    (CUDA_R_4F_E2M1 + VEC16_UE4M3, 两个 FP32 global scale 折进 alpha)。
- 测试 `tests/quant_fp4_test.cpp` 9 项: e2m1 解码表 / e2m1 编码 round-trip
  (含 tie 与饱和) / e4m3 round-trip (含 448 与单调性) / swizzle 偏移 /
  swizzle 大小 / swizzle round-trip / dequant kernel / 激活量化 kernel /
  **W4A4 GEMM 8 例** (真实 expert 形状 N=640 K=2560 与 N=2560 K=640 ×
  M=1,8,64,256, max_rel < 2e-5)。全量 35 项测试通过, 零警告。

**为什么重要**
- 量化层核心就绪: 模型层可直接调用 `Fp4Gemm` (W4A4 routed expert) 与
  `DequantFp4ToBf16` (W4A16 备选), 激活量化 kernel 在 forward 里把 BF16
  激活转成 NVFP4 喂给 cuBLASLt。
- 确认 W4A4 数值正确 (max_rel < 2e-5, 纯 FP4 量化噪声), 用户要求的
  "原生 NVFP4 利用硬件特性" 路径在库级别打通。

**踩坑 (device 端 constexpr 查表)**
- **constexpr 数组在 device 代码无存储**: `format.h` 初版用
  `inline constexpr float kE2m1Table[16]` / `kE4m3Pow2[15]` 做运行时索引
  解码。CMake 构建 (带 `--expt-relaxed-constexpr`) **编译通过**, 但
  namespace 作用域 constexpr 数组在 device 端没有存储, 运行时索引读到
  垃圾 → dequant/act_quant kernel 输出全 0。而 W4A4 GEMM 测试仍 PASS
  (cuBLASLt 用硬件自己的 e4m3 解码, 不调用我的函数), **掩盖了 bug**。
  修复: 解码改无查表 (按位 + `ldexpf`), 全部 `__host__ __device__`。
- **教训**: ① device kernel 里不要用运行时索引的 constexpr 数组
  (编译不报错, 静默错值); ② 单元测试必须直接验证 kernel 输出, 不能只
  靠端到端 GEMM (会掩盖底层格式错误)。
- **UE4M3 最大值**: 一度误以为是 0xFF=480 (全 unsigned 范围), 查 CUTLASS
  `float_ue4m3_t` 注释确认 Range [0:448]、has_NaN: true, 即与 e4m3fn
  正数布局一致 (0x7E=448, 0x7F=NaN)。
- **e2m1 tie 边界**: round-to-nearest-even 把中点 (0.25/1.25/2.5/5.0) 归
  到偶数 mantissa 码 (0/2/4/6), 故这些中点用 `<=`, 其余 (0.75/1.75/3.5)
  用 `<`。
- **`__nv_bfloat16` 无 `.x` 成员**: `.x` 在 2-wide 类型上; 单值用
  `*reinterpret_cast<const uint16_t*>(&b)` 取 bits。

**下一步**
- 量化层收尾: 权重 NVFP4 加载编排 (packed + swizzled scale 直载进 4 个
  大 buffer, 参考 qwen35-thor 的 direct-to-packed) / grouped MoE GEMM
  (512 expert top-10 + shared) / input_scale 在 forward 接线。
- 然后进入模型层 (48 层 forward)。

---

## 2026-09-04 — 量化层前置验证: cuBLASLt 原生 NVFP4 (W4A4) 跑通

**做了什么**
- 按用户要求 (利用硬件特性, 不做软件模拟) 验证 cuBLASLt 原生 NVFP4
  在 Thor SM110a 上是否可用: `CUDA_R_4F_E2M1` 主数据 +
  `CUBLASLT_MATMUL_MATRIX_SCALE_VEC16_UE4M3` scale 张量, W4A4
  (权重与激活都 NVFP4, 对应 checkpoint 的 input_scale)。
- 验证程序 `.q4t-work/fp4_validate.cpp` (throwaway, 不进构建): host 端
  把随机 BF16 量化成 NVFP4 (group-16 e4m3 scale + e2m1 值 + FP32
  global scale), 跑 `cublasLtMatmul`, 与 CPU dequant+FP32 GEMM 参考
  对比。真实 expert 投影形状 (gate/up N=640 K=2560, down N=2560
  K=640) × M=1,2,4,8,16,64,128,256 共 16 例。
- **结果: 16/16 通过, max_rel < 0.0001** (纯 FP4 量化噪声, 无布局错误)。

**为什么重要**
- 确认原生 NVFP4 硬件路径可行, 量化层可以建立在 cuBLASLt 之上而非
  手写 dequant-to-BF16 (qwen35-thor 走的是 W4A16 软件模拟, 有
  `TODO: Try native cuBLASLt FP4 path`)。
- 为 W4A4 (routed expert) 与可能的 prefill GEMM 铺路。

**踩坑 (NVFP4 scale 布局)**
- **scale 张量不是行主序**。初版用行主序 `[N, K/16]` 上传, matmul 能跑
  但 0/16 通过 (max_rel 14~72)。根因: `VEC16_UE4M3` 要求硬件
  tcgen05.mma 的 **128 行 × 64 元素 swizzle atom** 布局 (CUTLASS
  `SfKMajorAtom`)。
- 用 CuTe `tile_to_shape(SfAtom, (M,K), Step<_2,_1>)` 探针
  (`.q4t-work/sf_probe.cpp`) 打印权威 offset 表, 推导出公式:
  `i=r%32, j=(r%128)/32, ga=g%4, within=i*16+j*4+ga,
  offset=within+(g/4)*512+(r/128)*((K/16)/4)*512` (g=K/16 组索引)。
- **物理大小要 padding 到完整 atom**: `ceil(rows/128)*ceil((K/16)/4)*512`。
  即使 M=8 也按 128 行分配, 否则 offset 写到 20479 而 buffer 只有 8*160
  字节 → `malloc(): mismatching next->prev_size` 堆越界 abort。
- 主数据 (FP4 packed) 保持行主序 `[N, K/2]`, 只有 scale 走 swizzle。
- K 必须是 32 的倍数 (K=16 时 heuristic status=15 NOT_SUPPORTED);
  真实形状 K=640/2560 均满足。
- CUTLASS 头文件与 nvcc 13.3 不兼容 (`__CUTLASS_UNUSED` 未声明),
  探针只 include `cute/tensor.hpp` 手动定义 atom 绕过。

**下一步**
- 量化层正式实现: e2m1 dequant kernel / 运行时激活量化 (input_scale) /
  权重 NVFP4 加载 (含 swizzle scale) / cuBLASLt W4A4 GEMM 封装 /
  grouped MoE GEMM (512 expert top-10)。

---

## 2026-09-04 — IO 层: tokenizer (GPT-2 Byte-Level BPE) + 参考项目补全

**做了什么**
- 实现 `include/q4t/text/tokenizer.h` + `src/text/tokenizer.cpp`
  (独立 `q4t_text` 静态库, 链接 ICU 74):
  - GPT-2 byte-level 字母表 (33-126/161-172/174-255 直通, 其余映射
    256+ 扩展码点) + 反向 byte_decoder。
  - fail-closed schema 校验: 固定 base vocab 248044 / merges 247587,
    pre_tokenizer (Sequence[Split/Regex, ByteLevel]) / decoder (ByteLevel) /
    normalizer (NFC) 逐字段精确匹配, added_tokens 动态解析 (目标模型 33 个,
    id 248044-248076)。
  - encode: ICU 74 NFC 规范化 → `\p{L}\p{M}\p{N}` 预分词正则切分 →
    每段 BPE (双向链表 + generation + rank 最小堆, 惰性删除失效候选)。
    added-token 做**整体子串匹配** (earliest 优先, 同位置 longest 优先),
    与 transformers/tokenizers 行为一致。
  - decode: base id → byte 符号串 → byte_decoder 还原字节; added id →
    content (skip_special_tokens 跳过 special=true 的 added token)。
- CMake: 新增 `q4t_text` 库 (pkg-config 探测 icu-uc/icu-i18n), 测试段
  受 `Q4T_HAS_ICU` 保护。
- 测试 `tests/text_tokenizer_test.cpp`: 4 项 (真实文件加载 + 维度断言 /
  15 个 golden encode case / round-trip / 特殊 token encode+decode)。
  共 26 项测试全绿。
- **差分验证**: 57 个多样化输入 (空串/纯空白/CJK/emoji/NFC 组合字符/
  特殊标记/长重复/标点/Unicode 符号) 与 python `tokenizers` 库逐位一致,
  0 不匹配。
- 参考项目补全: 克隆 Qwen3x-Orin (1688f50, tokenizer 权威参考) /
  thor-bench (3a33a90) / thor-probe (4816685) 到 reference/, 更新
  REFERENCE.md (commit + Qwen3x-Orin tokenizer 研读要点)。

**踩坑**
- **精简 ICU 安装缺 C++ 类头**: 系统 ICU 74.2 只有 C API (`uregex.h` /
  `uregex_*`), 没有 `regexpattern.h` / `regexmatcher.h` (C++ 类)。改用
  `uregex_open/setText/find/findNext/start/end/close` C API 实现正则,
  `UnicodeString` (unistr.h) + `Normalizer2` (normlzr.h) 仍可用。
- **终端把 ASCII 特殊标记渲染成 CJK**: 目标 tokenizer 的 added token
  content 实为标准 Qwen ASCII 标记 (248044=<|endoftext|>, 248045=<|im_start|>,
  248046=<|im_end|>, 248059=</tool_call>), 但终端显示成 CJK 字形, 一度误判为 CJK 内容。
  教训: 涉及特殊字符时一律用 hexdump / 字节转储确认真实字节, 不信任
  终端渲染。
- **heredoc 损坏非 ASCII 字符**: 通过 `python - <<'PY'` 传递含 CJK /
  特殊标记的字符串时字节被破坏, 产生假的 encode 结果 (一度误判
  "encode 不做 added-token 匹配")。改用 create_file 写脚本 + 从
  tokenizer.json 动态读取 added token content, 彻底规避。
- **CMake 变量 vs 编译定义混淆**: 把 `Q4T_HAS_ICU` 只写进
  `target_compile_definitions` 字符串, 没作为 CMake 变量 `set()`,
  导致 `if(Q4T_HAS_ICU)` 恒假, tokenizer 未被编译。
- `UnicodeString::buffer()` 不存在, 应为 `getBuffer()`; `uregex_*` 的
  status 参数须传 `UErrorCode*` 指针。

**下一步**
- IO 层已全部完成。进入**量化层** (NVFP4 W4A4 / FP8 原语), 随后模型层
  (48 层 forward)。

---

## 2026-09-03 — IO 层: 权重加载编排 (WeightIndex + WeightLoader)

**做了什么**
- 实现 `include/q4t/io/weight_loader.h` + `src/io/weight_loader.cpp`:
  - WeightIndex: 解析 model.safetensors.index.json 的 weight_map
    (name → shard 文件) + metadata.total_size; ShardGroups() 按 shard
    分组 (保留 index 顺序)。
  - WeightLoader: 相对 model_dir 解析 shard 路径, 按需 mmap 打开
    (SafetensorsFile), LRU 缓存 (max_open_shards, 默认 8); FindTensor /
    ReadTensor / ReadTensorToDevice 按张量全名读取。
- 测试 `tests/io_weight_loader_test.cpp`: 3 项 (真实 index 解析: 296347
  张量 / 197 shard / total_size 83995036096; 读取与直接打开 shard 逐字节
  一致; LRU 容量 1 时驱逐)。共 22 项测试全绿。

**踩坑**
- 方法名 `TensorInfo` 与类型 `TensorInfo` 同名触发 `-Wchanges-meaning`
  (成员函数遮蔽了类型名), 改名为 `FindTensor`。
- C++ 默认参数不能位于最后一个参数之前 (`Create(..., size_t=8, T** out)`
  非法), 移除默认值由调用方显式传入。

**下一步**
- IO 层续: tokenizer (tokenizer.json 解码)。完成后 IO 层齐备, 进入量化层。

---

## 2026-09-03 — IO 层: config.json 解析 (ModelConfig)

**做了什么**
- 实现 `include/q4t/io/model_config.h` + `src/io/model_config.cpp`:
  把 config.json 的 text_config 超参解析到 `ModelConfig` 结构体, 含
  RopeParams / MtpConfig / QuantConfig 子结构, 以及派生方法
  (num_full_attention_layers / IsFullAttention)。
- 覆盖字段: 核心维度 (48 层 / hidden 2560 / vocab 248320 / 262K ctx)、
  full attention (24 头 / 2 KV / head_dim 256 / interval 4)、MoE
  (512 专家 / top-10 / inter 640)、linear attention (DeltaNet 头/维/conv)、
  QSA indexer (budget 2048 / compress 4)、PLE (ngram 3 / 8 头 / embed
  2560 / layer_ids [2] / vocab base 2e7)、hyper-connection (hc 4 / lowrank
  320)、MRoPE (section [11,11,10] / partial 0.25 / theta 1e7)、MTP (1 层
  full_attention)、NVFP4 量化 (ignore 列表)、token ids。
- 不变量校验: model_type 必须 qwen4_exp、layer_types 长度 == 层数、
  至少 1 个 full_attention、ple_embed_dim 是 ngram_heads 的倍数。
- 测试 `tests/io_model_config_test.cpp`: 2 项 (真实 config.json 全字段
  断言; 缺失文件报错)。共 19 项测试全绿。

**下一步**
- IO 层续: tokenizer (tokenizer.json 解码) + 权重加载编排。

---

## 2026-09-03 — IO 层核心: JSON 解析器 + safetensors mmap 读取器

**做了什么**
- 实现 `include/q4t/io/json.h` + `src/io/json.cpp`: 最小递归下降 JSON
  解析器 (对象/数组/字符串含 \u 转义与 surrogate pair/数字/true/false/
  null), 产出小型 Json 值类型 (带 GetInt/GetString/GetArray 等访问器),
  错误带偏移定位。无外部依赖, 复用于所有模型 JSON 文件。
- 实现 `include/q4t/io/safetensors.h` + `src/io/safetensors.cpp`:
  mmap 只读 safetensors 读取器。解析 8 字节头长 + JSON 头 + 数据区,
  提取每张量 dtype/shape/data_offsets; 按需读字节 (ReadTensor) 或
  H2D (ReadTensorToDevice)。Dtype 支持 F64/F32/F16/BF16/I64..I8/U8/
  BOOL/F8_E4M3/F8_E5M2。
- CMake: 独立 `q4t_io` 静态库 (json.cpp + safetensors.cpp, 链接
  CUDA::cudart)。
- 测试: `tests/io_json_test.cpp` (4 项: 标量/嵌套数组/字符串转义/错误
  定位) + `tests/io_safetensors_test.cpp` (2 项: 合成文件解析+读取;
  真实模型 scale 文件头解析)。共 17 项测试全绿。

**踩坑**
- C++ raw string 定界符 `R"json(...)json"` 易写错 (结尾须 `)json"`);
  测试里改用普通转义字符串更清晰。
- 测试断言字符串字节数时, \u4E2D 解码为 3 字节 UTF-8, 需精确计数。

**下一步**
- IO 层续: config.json 解析 (超参→结构体) / tokenizer / 权重加载编排。

---

## 2026-09-03 — PLE 端到端 gather (PleEmbedding) 实现并通过真实文件验证 ★

**做了什么**
- 研读 sglang-ssd-stream 的 `reduce` (backend.py: TP=1 时 no-op) 与
  qwen4.py 的 lookup 准备/收尾, 确认输出布局 = [tokens, 2560]
  (16 head × 160 直接拼接, reader 已按 token-major 顺序 scatter),
  `weight_scale` 在 reduce 之后单独乘 (不在 gather 内)。
- 实现 `include/q4t/ple/ple_embedding.h` + `src/ple/ple_embedding.cpp`:
  PleEmbedding 编排类, 组合三个已验证构件:
  - ComputeRowIds (CPU, 纯函数): tokens + 每 token 2-token history →
    [n_tokens, 16] row_ids (token-major)。
  - GatherRows: PlePageReader 读 pinned staging → H2D → FP8→BF16 (stream)。
  - Gather: 组合 (1)+(2), 用 pinned host row_ids scratch 中转。
  - pinned staging (cudaHostAlloc) + GPU FP8 scratch (cudaMalloc),
    按 capacity_tokens 预分配。
- 测试 `tests/ple_e2e_gather_test.cpp`: 2 项 (与 CPU 参考对比全链路布局+
  数值; capacity 越界守护)。共 11 项测试全绿。
- **真实环境最终验证 (硬性约定 #7)**: 用真实 checkpoint 参数
  (multipliers/vocab/offsets/EOS) + 真实 51.2 GB sidecar 跑 Gather,
  6 token (含 1 个 EOS 边界) × 16 head × 160 字节, 与 pread 真实文件 +
  e4m3 解码逐字节一致。

**结论**
- **PLE 流式层 (核心特性) 全部完成**: ngram 哈希 / io_uring 读取器 /
  FP8→BF16 转换 / 端到端 gather, 每个构件 + 整条链路均在真实
  checkpoint 参数与真实 51.2 GB 文件上验证通过。

**踩坑**
- 手动 g++ 链接验证程序时, cuda_runtime.h 在
  `/usr/local/cuda-13.3/targets/sbsa-linux/include` (非顶层 include),
  需显式 -I 该路径。

**下一步**
- IO 层: safetensors 解析 (mmap) + JSON 配置 + tokenizer。

---

## 2026-09-03 — PLE FP8→BF16 CUDA 转换 kernel 实现并通过 GPU 验证

**做了什么**
- 研读 sglang-ssd-stream 的 Triton 转换 kernel (backend.py
  `_copy_ple_staged_rows_kernel`) 与 PLE 层 forward (qwen4.py),
  确认: 转换 kernel 只做 **FP8 e4m3 → BF16 纯类型转换**, `weight_scale`
  在 PLE 层 forward 的 16-head reduce 之后单独乘
  (`embeddings = reduce(rows) * weight_scale`)。因此 kernel 保持纯转换,
  与参考一致。
- 实现 `include/q4t/ple/fp8_convert.h` + `src/ple/fp8_convert.cu`:
  ConvertFp8ToBf16Async (每线程 1 字节, 用 `__nv_cvt_fp8_to_halfraw`
  官方 e4m3 解码, 在 side stream 上启动)。
- CMake: fp8_convert.cu 加入 q4t_ple, 链接 CUDA::cudart (头文件暴露
  CUDA 运行时)。
- 测试 `tests/ple_fp8_convert_test.cpp`: 2 项 (与 CPU e4m3fn 参考解码
  对比 13 个构造字节: 零/次正规/正规/负/最大有限/NaN; 空输入 no-op),
  真实 GPU 上逐字节一致。共 9 项测试全绿。

**踩坑**
- `__half` 无 `.x` 成员/默认构造, 需用 `__half(hraw)` 从 `__half_raw`
  构造。
- `std::ldexpf` 在 C++17 不可用, 用 `std::ldexp`(double) 转 float。
- 顺带修复 `ngram_hash_derive.cpp` 的 `-Woverflow` 警告: `1LL << 63`
  是未定义行为, 改用 `std::numeric_limits<int64_t>::max()`。

**下一步**
- PLE 端到端 gather: ngram 哈希 → reader → 转换 → weight_scale →
  key/value 投影, 对真实 51.2 GB 文件验证。

---

## 2026-09-03 — PLE io_uring SSD 读取器实现并通过真实文件验证

**做了什么**
- 研读 sglang-ssd-stream 的 `src/lib.rs` gather() 逻辑, 精确理解
  页切片/去重/批量波次读取/scatter 语义。
- 实现 PLE SSD 页读取器:
  - `include/q4t/ple/page_reader.h` + `src/ple/page_reader.cpp`:
    PlePageReader (Create/Gather/ReadStats)。
  - 行→4KiB 页对齐切片 (Piece{page_id, output_offset, page_offset, len})
    → 按 page_id 排序去重分组 (PageGroup) → io_uring 批量读
    (4096 页/批, 256 页/波) → scatter 到输出; 越界行输出置零。
  - 32MiB 注册页池: mmap(MAP_PRIVATE|MAP_ANONYMOUS) + MADV_DONTDUMP
    + io_uring_register_buffers (失败回退普通 Read)。
  - 文件 POSIX_FADV_RANDOM 提示。
- 测试 `tests/ple_page_reader_test.cpp`: 4 项 (基本行/页去重/越界置零/
  跨页行) 全过; 连同 ngram 哈希共 7 项测试全绿。
- 真实环境验证 (硬性约定 #7): 在真实 51.2 GB sidecar 上读 7 行
  (0/1 同页、25/26 同页、1000000、320001000、末行 320001535),
  与直接 pread 逐字节一致; 7 行 → 5 个唯一页 (去重正确)。

**踩坑 (重要)**
- `io_uring_submit_and_wait` 成功时返回**实际提交的 SQE 数**(≥0),
  不是 0; 初版用 `!= 0` 判断导致误报失败。正确判断: `< 0`。
- ring 版 buffer 注册函数是 `io_uring_register_buffers(ring, iov, nr)`,
  不是全局 `io_uring_register(fd, ...)` (后者是 fd 版)。
- 终端 cwd 反复被重置到无关目录, `cd` 被工具剥离; 一律用绝对路径
  (cmake -S /abs -B /abs, git -C /abs)。

**下一步**
- FP8→BF16 CUDA 转换 kernel (side stream)。
- PLE 端到端 gather: ngram 哈希 → reader → 转换 → key/value 投影,
  对真实文件验证。

---

## 2026-09-03 — Phase 1 启动: PLE ngram 哈希实现并通过测试

**做了什么**
- 检查 checkpoint 张量布局: 确认 PLE 派生 buffer 全部存在
  (`layer_multipliers` I64[3]、`ngram_heads_vocab_sizes` I64[16]、
  `ngram_heads_offsets` I64[16]、`weight_scale` BF16[1]), 运行时直接
  加载即可, 无需 sympy/splitmix64 重新推导。PLE 权重在 0-indexed
  `layers.1.ple.*` (ple_layer_ids=[2] 是 1-based)。
- 读取真实 checkpoint 数值: multipliers=[23703573157769,
  20109073645365, 8052911324071], 16 个素数词表 (20000003..20000171),
  offsets 累加; 验证 offsets[-1]+vocab[-1]=320001446 < sidecar 行数
  320001536 (差 90 = 向上取整到 128 倍数), 完全自洽。
- 用 SGLang 精确算法 (Python) 生成 5 组参考 row_ids 作为测试基准。
- 实现 PLE ngram 哈希:
  - `include/q4t/ple/ngram_hash.h` + `src/ple/ngram_hash.cpp`:
    ComputeNgramRowIds (含 EOS-ignoring shift 规则)。
  - `include/q4t/ple/ngram_hash_derive.h` + `.cpp`: splitmix64 派生
    multipliers (开发期交叉校验)。
  - 公共基础设施: `include/q4t/{status,log,test}.h`, 测试框架
    (Q4T_TEST/Q4T_CHECK, 无外部依赖)。
  - CMake 重构: 抽出 `q4t_ple` 静态库 + `q4t_tests` 可执行。
- 测试 `tests/ple_ngram_hash_test.cpp`: 3 项全过。

**踩坑 (重要)**
- 初版哈希把乘子 m[k] 乘到"较旧"的 token 上, 2-gram 全对但 3-gram 错。
  根因: SGLang 的 `_shift_right_ignore_eos` 不只是移位——**窗口内存在
  EOS 时, 跨越 EOS 边界的旧 token 会被替换成 EOS**。修正: 乘子 m[k]
  对应的 token (往回数第 k 个) 仅当它与当前 token 之间无 EOS 时取真实值,
  否则取 EOS。修正后与 SGLang 参考逐位一致。
- 验证方法: C++ 必须与 SGLang 参考算法在真实 checkpoint 参数下逐位
  一致 (5 组窗口含 EOS 边界), 这是 PLE 正确性的硬基准。

**下一步**
- io_uring SSD 读取器 (页去重 + 32 MiB 注册页池 + 4 KiB 页映射)。
- FP8→BF16 CUDA 转换 kernel。
- PLE 端到端 gather (对真实 51.2 GB 文件验证)。

---

## 2026-09-03 — PLE 机制破解: 研读 sglang-ssd-stream + SGLang qwen4_exp

**做了什么**
- 精读 `reference/sglang-ssd-stream` (qwen4.py / backend.py /
  lib.rs), 并拉取 SGLang `qwen4_exp.py` (commit 0a79825, 即
  sglang-ssd-stream 为 aarch64/Thor pin 的版本) 固化到
  `reference/sglang-qwen4-exp/` (含 PROVENANCE.md)。
- **完全破解 PLE 机制** (详见 MODEL.md):
  - PLE = **n-gram 哈希查找表**, 不是普通逐层嵌入。
  - 每 token 取 [t-2,t-1,t] 3-gram 上下文 (前 2 个来自
    per-request 2-token 历史缓存, EOS 边界不跨越)。
  - 16 个 head (2 阶 × 8): 每阶用 splitmix64 派生的奇数乘子
    做 XOR 混合, 对素数词表 (nth_prime_after(20M-1, h+1))
    取模 + offset → row_id。16 个词表之和 = 320,001,536 = 表行数。
  - 查 16 行 (160B FP8) → 2560 维 BF16 嵌入 →
    key_proj(→10240) / value_proj(→2560) → 与主干 4 分支
    hyper-connection hidden 做门控 (sigmoid 平滑) →
    depthwise conv1d (k=4, dilation=2, 零初始化, per-request
    state) → silu → 加到主干 (attn_hyper_connection.mix 之前)。
  - PLE 在 layer_id=2 (ple_layer_ids=[2]), 主模型 forward 循环
    中 layer i 执行前 prefetch layer i+1 的 PLE (独立 stream 重叠)。
- **确认 hc_*/indexer_* 语义**:
  - hc_count=4: 主干是 4 分支 hyper-connection (GatedResidual,
    每层 attn/mlp 各一个 mix/combine, 末尾 mixer 合成 2560)。
  - indexer_*: full_attention 层用 QSA 稀疏注意力 (indexer 算
    top-k KV 索引, budget=2048, compress_ratio=4), 注意力输出
    带 sigmoid gate。
- SSD Stream 读取机制确认: GPU ids → pinned host → 单线程
  io_uring 批量读 (4KiB 页去重, 32MiB 注册页池, FADV_RANDOM)
  → 2×16MiB pinned staging 轮转 → 独立 CUDA stream FP8→BF16
  → consumer wait event。

**为什么重要**
- PLE 是本项目核心特性, 此前 row_id 计算与融合方式完全未知,
  是最大技术风险。现已有权威参考 (SGLang 源码), 可精确复刻。
- 发现 full_attention 是 QSA 稀疏注意力 (非标准 dense GQA),
  实现复杂度高于预期, 已列入风险。

**下一步**
- 开始 Phase 1 实现 (顺序: IO → 量化 → PLE 流式 → 模型 → 引擎
  → 服务)。full_attention 前拉取 SGLang qsa 模块; linear_attn
  前研读 qwen35-thor deltanet。

---

## 2026-09-03 — 骨架落地: 构建验证 + 参考克隆 + 推送 GitHub

**做了什么**
- 建立完整目录骨架 (include/q4t, src/{core,io,quant,text,model,
  runtime,kernels,ple,vision,server,mtp}, tests, tools, reference)。
- CMake 构建骨架: C++17/CUDA (SM110a), `-Wall -Wextra`,
  `find_package(CUDAToolkit)`, liburing 检测 (pkg-config)。
  产物 `build/q4t`。
- `q4t version` / `q4t probe` 实现并验证: probe 正确识别
  NVIDIA Thor (CC 11.0, 20 SM, 122.9 GB, L2 32 MB, 228 KB smem/SM,
  1048 MHz)。`generate` / `serve` / `models` 为 stub (exit 2)。
- 克隆参考项目到 reference/ (gitignore, 只读):
  - sglang-ssd-stream @ 176a522 (v0.2.0)
  - qwen35-thor @ 57e2977 (不含 submodule)
- 安装 liburing 2.5 (liburing-dev, apt)。
- git init (main) + 首次提交 + 推送
  github.com/thomas-hiddenpeak/qwen4-thor (private)。

**踩坑记录**
- 终端会话 cwd 会被重置回 /home/rm01/Orator, `cd` 不可靠;
  一律使用绝对路径 (`cmake -S <abs> -B <abs>`, `git -C <abs>`)。
- CUDA 13 移除了 `cudaDeviceProp::clockRate` / `maxClockRate`,
  改用 `cudaDeviceGetAttribute(cudaDevAttrClockRate)`。

**下一步**
- 研读 reference/sglang-ssd-stream (PLE 机制) 与 transformers 5.8
  的 qwen4_exp 实现, 消解 MODEL.md 中的 [待确认] 项。

---

## 2026-09-03 — 项目初始化

**做了什么**
- 完成模型与四个参考项目的调研 (qwen35-thor / Qwen3x-Orin /
  thor-probe / thor-bench / sglang-ssd-stream)。
- 确认目标: 完全独立的新项目, C++17/CUDA, 第一版即含多模态,
  PLE SSD Stream 为核心特性, 直接自研 (不依赖 sglang-ssd-stream
  运行时), HTTP API 纳入 Phase 1, 以真实使用环境为准开发测试。
- 确认环境: Jetson AGX Thor (SM110a), 122 GB 统一内存,
  CUDA 13.3, CMake 3.28, GCC 13.3 (aarch64)。
- 模型下载完成: 140 GB, 含 51.2 GB PLE sidecar
  (`ple/qwen3.8-flash-next-ple-fp8.bin`)。
- 建立仓库骨架: .gitignore / .clang-format / LICENSE (MIT) /
  README / AGENTS.md / docs 文档体系 / CMake 构建骨架。

**关键决策**
- 不采用 Qwen3x-Orin 的 SDD/constitution/proof contract 治理
  机制 (用户明确不喜欢, 过度强调证据、忽略工程本质)。
  改用轻量文档体系: STATUS (快照) + LOG (追加式日志) +
  ARCHITECTURE + PHASES + MODEL + REFERENCE。
- 验证标准 (正确性如何判定) **暂不定义**, 后续单独讨论;
  初步方向是与 sglang-ssd-stream 的 greedy 输出对比。
- 参考项目源码放 `reference/` (只读, 不参与构建), 便于随时
  查阅实现细节。
- 代码风格沿用 Orator 的 Google C++ Style 约定
  (2 空格 / 80 列 / 指针靠左 / member_ 尾下划线 / I 前缀接口)。

**模型架构要点 (来自 config.json, 详见 MODEL.md)**
- `qwen4_exp`: 48 层 (36 linear_attn + 12 full_attn, 每 4 层一个
  full), hidden 2560, MoE 512 专家 top-10 + shared expert
  (intermediate 640), 路由专家 NVFP4 W4A4, 其余 BF16。
- PLE: 51.2 GB FP8 查找表 (320,001,536 行 × 160 字节),
  sidecar 文件, 每 token 16 次确定性查找。
- MTP: 1 层 full_attention, hybrid=true。
- Vision: 27 层 ViT, hidden 1152, patch 16, temporal_patch 2。
- 上下文 262K, MRoPE interleaved section [11,11,10]。

**下一步**
1. 克隆 sglang-ssd-stream 到 `reference/`, 研读 PLE SSD Stream
   实现与 row_id 计算。
2. 研读 transformers 5.8 的 qwen4_exp 实现, 确认 forward pass
   中 PLE 位置。
3. 更新 MODEL.md, 开始 Phase 1 实现。

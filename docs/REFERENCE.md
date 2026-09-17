# REFERENCE.md — 参考项目说明

> `reference/` 目录存放外部参考项目源码 (只读, 不参与构建,
> 不修改)。本文说明每个项目取什么、怎么用。

## 目录约定

```
reference/
├── sglang-ssd-stream/    # PLE SSD Stream 机制参考 (必读)
├── sglang-qwen4-exp/     # SGLang qwen4_exp.py 单文件 (PLE/forward 权威参考)
├── vllm/                 # vLLM main, 含完整 qwen4_exp 实现 (MTP/QSA/PLE 权威参考)
├── ds4/                  # DwarfStar: 同模型(qwen4_exp)+同硬件类(DGX Spark) C+CUDA (最直接对标)
├── tokenspeed/           # LightSeek: qwen4_exp Day-0, datacenter Blackwell (算法/架构参考)
├── flashinfer/           # 注意力 kernel 参考 (Blackwell FMHA / sparse / FP4 MoE)
├── flash-attention/      # FA2/FA3/FA4 (FA4 = Blackwell CUTE DSL, 含 block-sparse)
├── qwen35-thor/          # 同硬件 Qwen3.5 引擎, 架构模式参考
├── Qwen3x-Orin/          # 生产级 tokenizer 参考 (ICU 74 + BPE + golden fixture)
├── thor-probe/           # 硬件探测方法 (可选)
└── thor-bench/           # 性能基线数据 (可选)
```

- 以 `git clone --depth 1` 获取, 固定 commit 记录在下方。
- **只读**: 不修改、不构建、不纳入本项目 CMake。
- 查阅后, 有价值的理解沉淀到 MODEL.md / ARCHITECTURE.md,
  而不是在 reference/ 里做笔记。

## sglang-ssd-stream (核心参考)

- 仓库: https://github.com/garnermccloud/sglang-ssd-stream
- 用途: PLE SSD Stream 的完整参考实现。
- **重点研读**:
  1. 每 token 16 个 row_id 如何计算 (与 ngram 的关系)
  2. 行 ID → 4 KiB 页的映射与去重逻辑
  3. io_uring 提交/完成队列管理, 32 MiB 注册页池
  4. 独立 CUDA stream 上 FP8→BF16 转换的同步点
  5. PLE 输出在 forward pass 中的融合位置
  6. 24-32 GB GPU 的 grouped CPU-offload 策略 (专家块预取)
- 固定 commit: `176a522ef9d6dbb5056ae1f467fe49af0f1258a5` (v0.2.0,
  2026-09-03 克隆, `--depth 1`)

## sglang-qwen4-exp (PLE / forward pass 权威参考)

- 文件: `sglang/srt/models/qwen4_exp.py`
- 仓库: https://github.com/sgl-project/sglang
- 固定 commit: `0a79825b7baa3e2aafd54e89097a5aba83d00b4e`
  (sglang-ssd-stream install.sh 中 aarch64/Thor 配置 pin 的 SGLang 版本)
- 获取: 2026-09-03 raw.githubusercontent 单文件拉取, 含 PROVENANCE.md
- 用途: **qwen4_exp forward pass 的最高事实来源**。PLE n-gram 查找
  (Qwen4ExpNGramEmbedding / Qwen4ExpPLELayer)、hyper-connection、
  QSA 稀疏注意力、MTP、权重名映射 (load_weights) 均以此为准。
- 注意: 该文件依赖 SGLang 运行时 (ForwardBatch / req_to_token_pool /
  qsa 模块等), 不能直接编译; 只作算法与张量布局参考。

## vllm (qwen4_exp 完整实现, MTP/QSA 权威参考)

- 仓库: https://github.com/vllm-project/vllm
- 固定 commit: `2902ca1` (main, 2026-09-05 克隆, `--depth 1`)
- 用途: **qwen4_exp 的完整 PyTorch 参考实现** (14,192 行), 填补
  sglang-qwen4-exp 单文件无法覆盖的部分 (依赖 SGLang 运行时)。
- 关键路径 (`vllm/models/qwen4_exp/`):
  - `nvidia/mtp.py` (461 行) — **MTP 权威参考** (之前搁置 MTP 的唯一阻塞):
    `fc_embedding`/`fc_hidden` 均为 per-branch `Linear(H,H)` [2560,2560]
    (无 10240→2560 降维), `pre_fc_norm_hidden` 对展平多流 [T, hc*H]
    做 GemmaRMSNorm; 主模型须输出 pre-final-mixer 多流 [T, hc*H] 给
    MTP 第一步 (scheme A); MTP 层 = full_attention + QSA indexer +
    512 expert MoE (与主干同构, checkpoint `mtp.layers.0` 即 layer 48)。
  - `nvidia/ops/qsa_indexer.py` (639 行) — QSA indexer 权威参考:
    `token_topk = indexer_budget = 2048`, `block_topk = token_topk //
    compress_ratio = 512`, logits = `sum_h relu(iq·ck)` (无 1/√hd 缩放,
    单调变换不影响 top-k), expand = top-512 块展开 + **当前 group 因果
    尾部** `tail_start=((pos+1)//4)*4, tail_count=(pos+1)-tail_start`
    (与本项目 `TopkSelectKernel` 修复后的语义一致, 已交叉验证)。
  - `nvidia/ops/qsa.py` (869 行) / `qsa_pre_indexer.py` (508 行) —
    稀疏注意力 kernel 与 pre-indexer 路径。
  - `nvidia/ple_layer.py` (927 行) / `common/ple.py` — PLE 层参考。
  - `nvidia/model.py` (1071 行) — 完整主干 (decoder layer / MoE / 权重
    映射 `_EXTRA_WEIGHTS_MAPPER`)。
  - `nvidia/hyperconnection.py` + `ops/hc.py` — HC (GatedResidual) 参考。
- 注意: vLLM 是 CUDA/ROCm 双后端 (nvidia/ 与 amd/ 目录), 本项目只参考
  nvidia/ 路径; vLLM 用 paged KV + torch.compile, 本项目是手写 CUDA
  kernel, 架构不同, 只取算法与张量布局语义。KV 方面本项目已实现
  Paged KV (按页 + 页表间接寻址, PD-ready 前提, 见 ARCHITECTURE.md
  "PD-ready 架构")。

## transformers 5.16.1 (qwen4_exp 官方实现, 文本主干 + 多模态权威参考)

- 位置: `refenv` venv 的 site-packages (`transformers/models/qwen4_exp/`,
  `modeling_qwen4_exp.py` 2707 行 + `modular_qwen4_exp.py` 1186 行 +
  `configuration_qwen4_exp.py` 334 行)。**不在 reference/** (pip 包, 非
  git clone), 版本 5.16.1 (2026-09-06 发现)。
- 用途: **qwen4_exp 的 HuggingFace 官方实现**, 与 vLLM 互补:
  - **文本主干完整** (Qwen4ExpTextModel 48 层): GatedDeltaNet / QSA
    indexer / MoE / GatedResidual / PLE (NGramEmbedding + PLELayer) 全部
    有 PyTorch 参考, 可作**逐 token 对参考验证**的 oracle (CPU torch 可跑,
    见 `.q4t-work/ref4_logits.py`)。
  - **多模态权威参考** (本项目之前缺失): `Qwen4ExpVisionModel` (27 层
    ViT: patch_embed 16 → 可学习位置嵌入双线性插值 2304 → rotary →
    vision blocks → patch_merger 2) + `Qwen4ExpForConditionalGeneration`
    (视觉特征 `masked_scatter` 进 `image_token_id=248056` 占位 + M-RoPE
    `get_rope_index` 按 `mm_token_type_ids` 算 3D position_ids,
    `rope_deltas` 缓存供增量 decode)。checkpoint 实际含 333 个视觉权重
    张量, `language_model_only: False`。
  - **无 MTP**: `_keys_to_ignore_on_load_unexpected = [r"^mtp.*"]` —
    加载时显式跳过 31 个 `mtp.*` 权重。MTP 权威参考仍是 vLLM。
- 与本项目实现的机制对比 (2026-09-06 逐项核对, **全部一致**):
  - GatedResidual: norm (grouped RMSNorm × (1+w)) / mix
    (`silu(down/hc)` → `sigmoid(up)` → `mean_b(gate*normed)`) / combine
    (`2*sigmoid(inject/hc)`) 逐项一致。
  - QSA indexer: 投影 + plain RMSNorm + partial RoPE (前 64 维) + 压缩 K
    (FP32 均值 → norm → RoPE at group start) + `sum_h relu(iq·ck)/√hd`
    + top-block 展开 + 当前 group 因果尾部, 语义一致 (实现方式不同:
    torch.topk vs 顺序扫描, eager vs online softmax)。
  - PLE: multipliers 派生 (splitmix64) / EOS-ignoring shift / 16 head
    素数词表取模 / gate (`sigmoid(√|dot|·sign)`) / dilated depthwise conv
    逐项一致。
  - MoE router: transformers `softmax(全部512) → topk → 重归一化`
    (norm_topk_prob=True 默认) 与本项目 `topk(logits) → 选中 k 上
    softmax` 数学等价 (softmax 单调, top-k 选择相同)。
- 注意: refenv 的 torch 是 **CPU-only** (2.14.0+cpu), 参考验证走 CPU
  路径; 全 48 层 MoE dequant (~242 GB) 超统一内存, 参考验证以 4 层
  (含首个 full_attention) 为基线 (见 LOG.md 2026-09-06 条目)。

## flashinfer (注意力 kernel 参考, prefill 优化主参考)

- 仓库: https://github.com/flashinfer-ai/flashinfer (Apache-2.0)
- 获取: 2026-09-15 `git clone --depth 1`
- 固定 commit: `c1c8e3e` (main, 2026-09-15)
- 背景: prefill 瓶颈定位在 QSA 稀疏 attention (nsys 2026-09-15:
  SparseAttentionKernel 78.8% + IndexerLogitsKernel 5.8%), 需要参考
  领先实现。Thor SM110a 为 Blackwell, 与 SM100/SM120 共享 tcgen05/TMA
  等核心特性, 这些代码路径可直接参考。
- **重点研读**:
  1. `include/flashinfer/attention/blackwell/fmha_cutlass_sm100.cuh` +
     `blackwell/{collective,kernel,device}/` — Blackwell FMHA (CUTLASS,
     tcgen05), 对应我们的 hd256 prefill attention
  2. `include/flashinfer/attention/sm120/nvfp4_attention_sm120/` —
     SM120 NVFP4 attention (FP4 Q/K/V, 与我们 W4A4 路线一致)
  3. `include/flashinfer/attention/hopper/sparse_mainloop.cuh` +
     `hopper/quantization/mainloop_sparse_load.cuh` — sparse attention
     mainloop (top-k KV 的访存组织, 对我们 764ms/call 的随机读延迟
     问题最相关)
  4. `include/flashinfer/attention/prefill.cuh` + `scheduler.cuh` —
     prefill 调度 (batch/ragged 布局)
  5. `csrc/sparse_mla_sm120_nvfp4_prefill.cu` — SM120 NVFP4 sparse
     prefill 实例
- 许可: Apache-2.0, 可参考/移植。

## flash-attention (FA2/FA3/FA4, attention 算法参考)

- 仓库: https://github.com/Dao-AILab/flash-attention (BSD-3)
- 获取: 2026-09-15 `git clone --depth 1`
- 固定 commit: `0dc2cb4` (main, 2026-09-14)
- 背景: 同上。FA4 是 Blackwell 的 CUTE DSL 实现, 且**原生支持
  block-sparse attention**, 与 QSA 的 top-k block 选择直接对应。
- **重点研读**:
  1. `flash_attn/cute/block_sparsity.py` + `block_sparse_utils.py` —
     **block-sparse 数据结构 (BlockSparseTensors: mask/full block
     分离)** 与 CUTE kernel 的消费方式, QSA top-2048 block 可直接
     映射
  2. `flash_attn/cute/flash_fwd_sm100.py` + `sm100_hd256_2cta_fmha_forward.py`
     — **hd256 (我们的 head_dim) Blackwell 前向**, 2-CTA 模式
  3. `flash_attn/cute/flash_fwd.py` (FA2 主体, Ampere+) — 经典
     online-softmax tiling, 我们手写 kernel 的对照基线
  4. `hopper/` (FA3, Hopper WGMMA) — 中间参考
- 许可: BSD-3, 可参考/移植。

## qwen35-thor (架构模式参考)

- 仓库: https://github.com/thomas-hiddenpeak/qwen35-thor
- 用途: 同硬件 (Thor SM110a) 的 Qwen3.5 完整推理引擎。
- **重点参考**:
  1. 目录分层 (engine/serve/kernels 的切分方式)
  2. DeltaNet SSM 实现 (WY chunked prefill, SSM state 管理)
  3. MoE grouped GEMM + shared expert (64 专家版, 我们 512)
  4. MTP 推测解码 (GPU-resident draft chain, partial accept)
  5. Paged KV cache + Split-K attention
  6. NVFP4 GEMV (SMEM LUT + vectorized loads)
  7. 双端口 HTTP server 与配置系统
  8. 权重加载 (adaptive mmap, direct-to-packed expert loading)
- 固定 commit: `57e29777c2aff8a97f42df6e3d9487b1327f014f`
  (2026-09-03 克隆, `--depth 1`, 不含 submodule)

## Qwen3x-Orin (tokenizer 参考)

- 仓库: https://github.com/thomas-hiddenpeak/Qwen3x-Orin
- 固定 commit: `1688f50ecc46e6e7c5696bcae541593c1aed6e2f`
  (2026-09-04 克隆, `--depth 1`)
- 用途: **生产级 GPT-2 BPE tokenizer 的权威参考实现**
  (`include/q3x/text/tokenizer.h` + `src/text/tokenizer.cpp`, 1534 行)。
- **重点研读**:
  1. ICU 74 用法: `icu::RegexPattern` 编译 `\p{L}\p{M}\p{N}` 预分词正则,
     `icu::Normalizer2::getNFCInstance` 做 NFC 规范化 (系统已装 ICU 74.2)。
  2. GPT-2 byte-level 映射 (`initialize_byte_mapping`): 33-126/161-172/
     174-255 直通, 其余映射到 256+ 扩展码点。
  3. BPE 优先队列 (`encode_bpe_piece`): 双向链表 + generation 计数 +
     rank 最小堆, 惰性删除失效候选, 高效正确。
  4. schema 校验 (fail-closed): 固定 vocab 248044 / merges 247587 /
     added_tokens 26, 逐字段精确匹配。
  5. 验证基准: `tests/fixtures/qwen36-27b-tokenizer.json` 用
     `tokenizers 0.22.2` 生成 golden encode cases (vocab 248044, 与目标
     模型一致)——可作 q4t tokenizer 的差分测试 oracle。
- 注意: 不采用其治理机制 (SDD / constitution / proof contract), 仅借鉴
  tokenizer 算法与 ICU 用法。

## thor-probe / thor-bench (按需)

- 硬件探测方法与 Thor 性能基线数据 (FP4 GEMM 595 TFLOP/s 等)。
- Phase 3 性能调优时参考。
- 固定 commit: thor-probe `481668514756b8decda421290c32dbbc130b897f`,
  thor-bench `3a33a90579acd5ab58f1df1f96c8df4b941a14a7` (2026-09-04 克隆,
  `--depth 1`)。

## ds4 / DwarfStar (最直接对标: 同模型 + 同硬件类)

- 仓库: https://github.com/antirez/ds4 (antirez/DwarfStar, MIT, 22.5k star)
- 固定 commit: `8db1d1d` (2026-09-16 克隆, `--depth 1`)
- **为什么最相关**: 唯一同时满足
  1. **同模型** —— 原生实现 Qwen3.8 Flash Next (`qwen4exp` 架构, gated
     delta-net + gated GQA + block-sparse attn + hyper-connections +
     n-gram + MoE + MTP), 与我们目标模型逐特性对应。
  2. **同硬件类** —— 主 CUDA target 是 **DGX Spark (GB10, Grace-Blackwell,
     LPDDR5x 统一内存 ~273 GB/s)**, 与 Jetson Thor (Blackwell SM110a +
     LPDDR5x 241 GB/s) 同为"弱 FP4 张量核 + 带宽受限统一内存"一类。
  3. **开源 C+CUDA** (llama.cpp 血统, 非 Triton), 可逐 kernel 读。
- **关键对标数据 (2026-09-17 读取)**: ds4 @ Spark 单流 prefill Qwen3.8
  Flash Next Q2/Q4 = **745-771 t/s** (8192 chunk); DeepSeek V4 Flash Q2 =
  820-872 t/s。我们 Thor NVFP4 W4A4 = **989 (T=2560) / 1143 (T=8000) t/s**
  → **我们单流 prefill 已快 ~30-50%** (Spark 带宽还略高), 印证 roofline
  "已近带宽地板 + 超同类开源 SOTA"。ds4 CUDA 只顺序 decode (无连续批处理),
  我们领先。
- **重点研读** (`ds4_qwen4_cuda.cuh`, 单文件 ~1800 行, 40+ kernel):
  1. `attention_group` (177+) — sparse attn, **tensor-core**
     (`tt_mma_m16n8k16` + `tt_ldmatrix` + `tt_cp_async_16B` KV 预取 +
     GQA-group 共享 key + 寄存器累加)。与我们 SparseAttention **同级**;
     它多一个 FP32-query hi/lo split (我们 bf16 不需要)。
  2. `gdn_prep`/`gdn_scan`/`gdn_out` (1653+) — GatedDeltaNet, **纯 SIMT
     递归** (印证 chunked tensor-core 是错路)。关键差异: **state 驻寄存器**
     (`s[ROWS][4]`, kd 分布到 warp lanes + warp-reduce, dv 分布到
     warp/block), 无 shared-state 占用率上限 —— 对照我们的 shared-state
     `S_smem[kd,vd]` (block-per-head, 25% 占用率锁死)。**潜在杠杆**。
  3. `moe_mv`/`expert_lists`/`expert_tiles`/`matrix_tc`/`matrix_reg` — MoE
     (router + 分专家收集 + tensor-core/寄存器 GEMM 变体)。
  4. `hc_*` (hyper-connections) / `conv`+`ngram_*` (PLE n-gram) /
     `mtp_*` (推测解码) / `vis_*` (Qwen3-VL 视觉塔)。
- **prefill 策略**: `--prefill-chunk` (Spark 默认 8192), continued-prefill
  分块; Metal `--batched-session` (权重读一次 + 递归/KV/conv per-session,
  正是我们 ModelPrefillBatch 思路), 但 **CUDA 未做批处理**。
- 许可: MIT (+ 保留 GGML 版权), 可参考/移植思路 (勿抄整段, 我们 NVFP4
  路线与其 GGUF Q2/Q4 不同)。

## tokenspeed / LightSeek (算法 + 架构参考, 非硬件迁移)

- 仓库: https://github.com/lightseekorg/tokenspeed (LightSeek 基金会, MIT,
  2.1k star)
- 固定 commit: `78c6518` (2026-09-17 克隆, `--depth 1`)
- **定位**: speed-of-light LLM 推理引擎 (TensorRT-LLM 级性能 + vLLM 级
  易用), Qwen3.8 Flash Next Day-0 支持。**主 target 是 datacenter
  Blackwell (B200/B300/GB300, HBM 3TB/s + 大张量核)** —— 与 Thor 硬件
  形态不同 (算力富余 vs 我们带宽受限), 故 **算法/架构参考价值 > 硬件
  kernel 迁移价值**。Python 94.8% + C++ 4.5% (Triton kernel)。
- **重点参考**:
  1. **control/execution plane 分离** (`tokenspeed-scheduler`): 控制面
     C++ 有限状态机 (请求生命周期 + KV cache 所有权在**编译期**用类型
     系统保证安全), 执行面 Python。架构思想可借鉴我们的 ModelSequence
     阶段机 + PD-ready 设计。
  2. `tokenspeed-kernel` — attention prefill 优化 (如 "skip fully masked
     sliding-window prefill tiles"); 但我们模型是 QSA block-sparse 非
     sliding-window, 且 Thor 带宽受限, tile-skip 收益结构不同。
  3. `tokenspeed-mla` — MLA (Multi-head Latent Attention), Blackwell 上
     领先实现; 我们模型非 MLA (是 gated GQA + linear attn), 仅算法参考。
  4. kernel registry / plugin 机制 (可移植 public API + 异构后端)。
- **注意 (同 session 教训)**: tokenspeed 的配平类技巧 (prefill/decode
  融合、chunked-prefill 交织) 依赖 datacenter 的 compute-vs-memory 流水线
  不平衡; Thor 均匀带宽受限, 这些**不迁移** (见 docs/log/2026-09-17.md
  融合负结果)。取其**架构设计**, 慎取其 datacenter-kernel 策略。

## 明确不参考

- Qwen3x-Orin 的治理机制 (SDD / constitution / proof contract /
  evidence chain)。用户明确不采用。仅其 tokenizer 算法与 API 设计思路
  可借鉴。

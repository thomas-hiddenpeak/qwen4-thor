# REFERENCE.md — 参考项目说明

当前工具收敛见[交付记录](MAIN_CONVERGENCE_2026-10-17.md)；近期研究及未实施计划见
[只读归档](ARCHIVED_RESEARCH.md)，不作为自动执行队列。

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
├── thor-bench/           # 性能基线数据 (可选)
└── pdf/                  # Qwen3.8-Next 官方架构技术报告 (PDF)
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
  6. 独立显存24-32 GB GPU的grouped CPU-offload策略，仅借鉴专家块
     预取/生命周期。Thor的CPU/GPU共用DRAM，不能移植为容量卸载；
     本项目专家容量后备仅SSD，见[合同](SSD_OFFLOAD_CONTRACT.md)。
- 固定 commit: `155a949` (v0.3.0, 2026-09-03 克隆, 2026-09-14 更新,
  `--depth 1`)

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
- 固定 commit: `6711197` (main, 2026-09-05 克隆, 2026-09-14 更新, `--depth 1`)
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

## DeepInfer

Engram / 数值一致性专项参考，暂不实施。

- 仓库：[deepseek-ai/DeepInfer](https://github.com/deepseek-ai/DeepInfer)。
- 固定 commit：`774ce244b87cc0eb8d4b94ab9f5de2ab22049f01`
  （2026-10-20浅克隆并静态评估；以下上游链接均固定该版本）。
- 本地临时快照：`.q4t-work/deepinfer-assessment-20261020/upstream/`；
  未纳入`reference/`或本项目构建，长期查阅以固定版本链接为入口。
- 结论：保留Engram与数值一致性验证方法；当前不展开实现、不启动
  新测试或性能采样，不改变STATUS所列主线。尚无Thor适配、质量或
  性能通过结论，也不作为qwen4_exp数学语义的权威参考。

### 设计学习：对本项目的启发（2026-10-23）

按用户要求从实际代码推导设计原则。本项目重读版本为
`4e3909438d83fbccbdf2241e1f6ff5c31a68b981`；DeepInfer、DeepEP及
TileKernels仍使用下文固定版本，未更新上游。以下“启发”为代码依赖与
生命周期分析，不是已测性能结论，也不形成实现或测试队列。
当前主线见[STATUS](STATUS.md)，容量解释遵循
[统一RAM/SSD合同](SSD_OFFLOAD_CONTRACT.md)；共享主机缓存保持搁置。

1. **先找依赖汇合点，再考虑工作放在哪一层。**

   代码事实：[DeepInfer forward][deepinfer-model]在层循环前hash/fetch，
   [Engram.forward][deepinfer-engram]到层才projection/gate。本项目
   [ComputeRowIds:131](../src/ple/ple_embedding.cpp)只依赖token、历史与固定参数；
   [PleLayerForward:635](../src/model/ple_layer.cu)的key/value投影及key norm
   也不依赖主干hidden，query norm/gate才读取对应层hidden。

   启发：把PLE理解为两条在gate汇合的依赖链：

   ```text
   本次token及其有效前缀 → row IDs → 表数据/缩放 → key、value、key norm ─┐
   前置层计算 ──────────────────────────────→ query norm ─┴→ gate/卷积
   ```

   地址已知的时点由输入前缀决定，不必与层函数的调用位置绑定。
   prefill输入已知、decode当前token选定、verify本轮候选确定后，分别
   具备地址计算条件；这不提供预测未知未来token的能力。
   本项目已有ComputeRowIds/GatherRows分离，尚无提前发起/到层消费流水。
   仅第二层有PLE，且当前Gather可能已与首层部分重叠，新增窗口未知。
   DeepInfer仍到层才投影，也说明“依赖允许提前”不等于“应抢先使用GPU”。
   本地投影结果来自共享workspace，提前计算会延长存活期、改变资源冲突；
   不能把移动调用位置当成收益结论。

2. **只读准备可以面向候选，状态提交必须属于确定的前缀。**

   代码事实：[上游scheduler][deepinfer-scheduler]将接受token同步写入原始/
   压缩历史。本项目[ModelSequence:38](../include/q4t/model/sequence.h)分开
   committed history与pending tokens；[MTP:1187](../src/mtp/mtp.cu)部分接受后
   恢复checkpoint，再推进已消费前缀。[PLE卷积](../src/model/ple_layer.cu)
   保存的是最近9项gated_n，不是最近两个原始token。

   启发：查表结果、词法历史、activation状态是三类对象。查表可以为
   已知候选做准备，未接受分支的结果可以丢弃；词法history与卷积状态
   则必须对应同一条序列的同一个已消费前缀。前两个原始token足以重建
   下一位置的地址；仅凭词法窗口或查表结果不能直接恢复依赖hidden/
   门控/舍入的卷积状态，仍需activation checkpoint或按相同数值合同
   重放所需前缀。取表完成也不表示状态可提交。
   本项目已有各路径context构造及PLE checkpoint，属于应保留的不变量。
   EOS屏障只影响哈希历史，chunk结束不清卷积，fresh/reset另有生命周期；
   不把这三种事件合并。若以后改变调度，序列、位置及前缀快照必须仍对应，
   失败时仅保住host history也不表示设备状态已自动回滚。

3. **异步接口的价值取决于所有权与背压，不取决于hook的外形。**

   代码事实：[DeepEP fetch][deepinfer-deepep-buffer]分issue/wait，但每轮
   复用固定接收区，hook不提供任意跨轮并发安全。本项目
   [GatherRows:154](../src/ple/ple_embedding.cpp)等待upload_complete后才让CPU
   覆盖staging；该事件覆盖H2D/转换，不覆盖输出BF16的下游消费。

   启发：分别解释读取完成、转换完成、消费完成及允许复用；选择线程、
   stream、event或hook应服务于这些边界。取消也要等所属工作退出才能
   交还缓冲。多加一套buffer只增加容量，不自动提供有界队列和正确背压。
   现有串行host调用/同stream顺序是有效前提，不能在引入并行时默认沿用。
   在Thor上增加host/device副本都会占统一RAM，CPU常驻不是容量卸载；
   上游RDMA存储只能贡献生命周期思路，不能继承其独立显存平台容量收益。

4. **保持记录身份与舍入边界，再自由组织物理读取和静态准备。**

   代码事实：[DeepEP issue][deepinfer-deepep-issue]将global row映射为
   peer/local offset；本地[page_reader:187](../src/ple/page_reader.cpp)
   按物理页排序/合并，同时保存
   output_offset并scatter回原token/head位置。DeepInfer将固定norm权重
   预合并；本地[PLE norm/gate](../src/model/ple_layer.cu)之间存在BF16
   写回，融合卷积也显式保留中间舍入。

   启发：可变化的是物理读取次序、分片位置及推理期不变量的准备时机；
   不变量是row ID、head顺序、FP8字节、scale解释及规定的数值边界。
   本地已有页合并和卷积/加法融合，学习价值在这种分层组织，不重复算作
   新能力。词表压缩会改变模型记录身份；权重代数预乘或合并GEMM可能
   改变舍入/算法，需单独判断，不能视为纯数据搬运。
   大表容量、每token有效payload与同时存活的工作集也要分开：
   本模型每token取2560个FP8字节，不需把51.2GB表整表搬入RAM；
   但页读取、重复副本、输出及工作区仍各有成本。

5. **固定前缀消除路径分叉，同时让验证结论与实际oracle匹配。**

   代码事实：上游[batch test][deepinfer-batch-test]把混批输出设为
   golden_token_ids；[sampler][deepinfer-sampler]实际覆盖输出token，
   再从本轮logprobs取对应值及top5。[NextN接受核][deepinfer-accept-kernel]
   还用golden与draft是否相等覆盖接受判定。因此这是受控前缀下的数值/
   布局对照，不独立证明两次自由生成或自然接受策略相同。

   启发：把“固定输入前缀时模型算得是否一致”与“采样/接受策略是否正确”
   分开，各自选独立参照。row ID可做整数精确对照，文件字节/转换、
   gate/conv/state及logits按各自计算合同分层，避免只比最终答案。
   本地[PrefixBoundaryReference:13](../tests/model_prefix_reference.h)
   只执行接受前缀，不调用checkpoint保存/恢复；单/多序列恢复测试用它
   比较后继输出。这已体现“参照不重复待验证机制”的组织原则，
   可与固定前缀跨布局方法互补，无需重复发明同一能力。
   本地[hash golden](../tests/ple_ngram_hash_test.cpp)验证地址数学；
   [gather测试](../tests/ple_e2e_gather_test.cpp)的参考也调用生产hash，
   主要独立核对取行/转换，不能再充当独立hash参照。
   [mixed测试](../tests/model_mixed_batch_test.cpp)仅2层且用容差；
   [triangle](../tests/mtp_path_triangle_test.cpp)仅4层，同形状T1对照
   进入返回条件，跨T1/T4与后缀变化只诊断输出。已有入口有复用价值，
   测试名字、打印结果或源码存在都不代表完整模型严格不变量已通过。

6. **收益和成本沿整条依赖链归因。**

   代码事实：[ReadStats](../include/q4t/ple/page_reader.h)统计页请求，
   但生产Gather尚未导出；[层计时](../src/model/model.cu)不能独立拆出
   完整查表链。[working_memory_bytes:104](../src/ple/ple_embedding.cpp)仅为
   对象口径，reader scratch按vector size计，不是保留capacity；模型
   输出/共享workspace另由[启动计划](../src/runtime/startup_model_plan.cpp)登记。

   启发：读请求字节、实际SSD流量、有效payload、等待时间是不同量；
   活动元素、保留分配与整机统一RAM也不同。预取可能只是把等待和内存
   存活期挪到前面，局部kernel减少也可能改变其他层资源竞争。
   本项目后续选择方案，应由完整服务成本和统一RAM目标解释价值；
   当前未测占比、额外RAM和E2E收益均保持未知，不从局部源码推算提速。

本轮出口是以上设计理解及源码入口；没有运行构建、测试、GPU/模型或
性能采样，也没有修改运行时、默认程序或建立后续自动执行队列。

### Engram：查表发起与层内消费分离

| 固定版本入口 | 重点参考 |
|---|---|
| [deepseek.py][deepinfer-model]：`DeepseekModel.forward` | 层循环前计算n-gram哈希并发起`engram_runtime.fetch`，向各层传递`embed_hook` |
| [v4_modules/engram.py][deepinfer-engram]：`EngramRuntime.fetch`、`Engram.forward` | 返回按层索引的hook；层内消费查表结果，再做融合KV投影与门控注入 |
| [parallel_utils/userbuffer.py][deepinfer-userbuffer]：`userbuffer.allocate_engram` | 经DeepEP `EngramBuffer`选择CPU RDMA存储，也支持强制GPU存储 |

与本项目PLE的n-gram查表、投影、门控注入相近，值得借鉴的是读取
发起/消费接口和缓冲所有权边界。其RDMA主机表与本项目NVMe、io_uring、
pinned staging不同；hook不证明延迟已完全隐藏。Engram的压缩词表、
哈希及门控数学不直接替代PLE语义，任何流水实现仍须独立验收。

### n-gram / PLE逐项对照（2026-10-20静态评估）

本项目对照源码：`d640bc97d034ae434e9a49ec003bd74a72fe9538`，
所查PLE路径无未提交改动。DeepInfer仍固定上述commit，继续追到其
`vendor/DeepEP` gitlink
`93eb6eb238127e96c6d7a4a625a6dad158348509`及
`requirements.txt`固定的`tile-kernels==2.1.0`，未用依赖main代替。
TileKernels核对的是[PyPI 2.1.0 wheel][deepinfer-tile-wheel]，SHA-256：
`df3ddffcf53ebc19404d2723bf2a629b46ce14cf4744a7739a80b0a9e366cf1f`。
依赖只下载/解包查阅，未安装、导入或执行；临时来源清单在
`.q4t-work/deepinfer-assessment-20261020/ngram-dependencies/sources.json`。

两者都以多阶、多head哈希查表，再通过投影和门控注入主干；这个
结构类比成立，但查到哪些行、如何反量化及注入的数学均属各自模型合同：

| 项目 | 本项目qwen4_exp PLE | DeepInfer Engram |
|---|---|---|
| token空间 | 原始token ID，不做词表压缩 | [CompressedVocabulary][deepinfer-ngram-meta]先逐token解码、Unicode/大小写/空白归一化并映射压缩ID；模型embedding仍使用原始ID |
| 窗口与哈希 | 2/3-gram各8头；当前token乘m0，前一乘m1，前二乘m2，XOR后按各头取模加offset；参数从checkpoint读取 | 同类XOR/素数表方法，但乘子由NumPy逐层RNG生成，表布局按其配置构造；不能复用其ID或生成器 |
| 边界 | 句首EOS248044填充；仅当EOS严格位于历史token与当前token之间，才屏蔽该历史token | [输入准备][deepinfer-ngram-input]用压缩PAD补缺失历史，图像sentinel屏蔽自身及更早历史；已查路径没有本项目的EOS中间屏蔽规则 |
| 表与量化 | NVMe FP8表，16×160字节拼接为2560维；FP8→BF16后乘checkpoint单一BF16 scalar并再次舍入 | CPU RDMA或GPU表分片；FP8模式每行每32通道UE8M0 scale，CUDA打包int32；取表与取scale路径分离 |
| 层内计算 | 两次BF16 GEMM得key/value，Gemma RMSNorm，门控后还有K=4、dilation=3短卷积；每通道9项历史及verify checkpoint | [Engram.forward][deepinfer-engram]融合KV投影、归一化/门控/残差注入，没有本项目的短卷积参数或状态 |
| 执行时序 | 到0-based layer 1才调用Gather；host页读取/scatter完成后排队上传、转换、缩放及PLE | 层循环前计算各层row IDs并发起读取，各层hook等待后消费 |

本地事实入口：[哈希](../src/ple/ngram_hash.cpp)、
[参数加载及到层查表](../src/model/model.cu)、
[页读取](../src/ple/page_reader.cpp)、
[缓冲所有权](../src/ple/ple_embedding.cpp)、
[投影/门控/卷积](../src/model/ple_layer.cu)。
原文档将乘子顺序、运行时参数来源和逻辑/物理行数混写的问题，
本次按代码修正到[MODEL](MODEL.md)；
属于描述纠正，没有改变模型计算。

**实际值得保留的参考：**

1. **读取发起/消费分离：参考价值高，收益未知。**
   [DeepEP接口及hook][deepinfer-deepep-buffer]在当前CUDA stream上
   发起fetch kernel；[issue kernel][deepinfer-deepep-issue]调用
   `gin.get`并`flush_async`，[wait kernel][deepinfer-deepep-wait]
   在对应层消费前等待completion。因此是真实异步传输，不只是延迟
   调函数；本路径也不是显式独立comm stream。接收区按层复用固定
   buffer，不能据hook外观推断任意跨stream/多轮并发安全。
   对本项目可借鉴发起、完成依赖与消费接口；需分别维护读取完成、
   H2D/转换完成、消费完成、允许复用的边界。现有staging/scratch保护
   不能删除，见[10-16修复](MTP_NUMERICAL_CLOSURE_2026-10-16.md)。
   单次forward内本项目仅第二层有PLE，可重叠窗口主要是第一层；
   当前到层Gather也可能已与第一层入队工作部分重叠，净新增窗口未知。
   把阻塞Gather简单移到循环前会推迟第一层入队，不等于实现prefetch。
   SGLang提前一层预取已是既有参考，不将此方法作为本次新增功能。

2. **合并KV投影：可保留候选，门控kernel不能照搬。**
   当前key/value仍两次GEMM，合并投影可作为后续研究入口，但改变GEMM
   形状可能改变算法和舍入，须按[投影参考](PLE_PROJECTION_REFERENCE_2026-09-23.md)
   区分数学等价与数值结果。TileKernels 2.1.0的
   `engram/engram_gate_fwd_cuda.py`用FP32累积norm/dot，根号为
   `sqrt(max(abs(raw), 1e-6))`，再直接计算残差；
   本项目为`sqrt(abs(raw)+1e-6)`且保留norm、gate、gated value、
   卷积与残差的BF16中间舍入，还需短卷积及回退状态。
   归一化权重预合并同样不能绕过这些数值边界；本项目已有卷积/加法融合，
   不能把Engram短路径全额算作可消除成本。

3. **固定前缀跨执行布局对照：参考价值高，本项目已有基础。**
   对照宜按token窗口→整数row ID→原始FP8行→转换/缩放→投影/
   gate/conv/state→logits分层，覆盖序列隔离、chunk边界和MTP部分接受。
   DeepInfer的[ZOS窗口修正][deepinfer-zos-window]及
   [只提交接受token的历史][deepinfer-scheduler]可作状态组织参考，
   但不提供本项目PLE卷积checkpoint的替代。
   已有[hash golden](../tests/ple_ngram_hash_test.cpp)及
   [真实查表内容核对](PLE_LOOKUP_REFERENCE_2026-09-23.md)可复用入口；
   后者覆盖旧版4102个token、10,501,120个FP8字节及转换/缩放，
   无内部EOS、未直接捕获CPU行号且当时整模型错答，不能外推当前整模型通过。

**参考价值有限或不可直接迁移的部分：**

- 已查DeepEP逐entry发起读取，请求聚合并非相同row去重，也未提供跨轮
  命中缓存；本项目已有4KiB页去重、io_uring分波和scatter，不能把这些
  再算成Engram带来的新增能力。CPU RDMA常驻分片也不等同运行期NVMe流式表。
- GPU哈希有实现参考，但本项目输入历史在host、NVMe读取计划也在host；
  迁到GPU可能新增row IDs回传。尚无CPU hash瓶颈证据，不能只因kernel
  存在就判断值得替换。词表压缩则会改变训练对应的表索引，不能作为
  无损存储或性能优化移植。
- 当前完整PLE的TTFT/decode占比未知：reader虽有ReadStats，生产调用
  尚未导出；PLE子计时仅包围层内计算。层计时相邻边界复用同一事件，
  PLE层起点又在查表/转换/缩放后，相关成本可能落入前层段，不能独立
  分离完整查表成本。`submitted_bytes`也不等于实际NVMe物理读取量
  （普通buffered I/O及OS页缓存）。因此不预测百分比收益，不把现有
  局部计时当完整查表成本。
- 结论是保留异步接口、投影组织和分层数值验证入口，暂不改实现、不新增
  GPU采样或验收队列；继续当前热启动/启动资源计划主线。

### 数值一致性：固定token路径比较执行方式

| 固定版本入口 | 验证方法及边界 |
|---|---|
| [test_batch_invariant.py][deepinfer-batch-test]：`_run_batch_invariant`、`_get_result` | 先混批生成，再以输出作为单条运行的`golden_token_ids`，严格比较token、token logprob、top-token IDs和top-logprobs |
| [test_kl.py][deepinfer-kl-test]：`_run_kl` | 先按golden token逐步decode，再将输出并入prompt重新prefill，比较所选token的logprob；当前夹具涉及图文、多卡及NextN |
| [worker/runtime_config.py][deepinfer-kernel-config]：`configure_worker_kernels` | batch-invariant模式为DeepGEMM与TileKernels启用deterministic算法 |
| [testing.py][deepinfer-diff]：`cal_diff` | `kl`为所选token logprob差值的`mean(exp(x-y)-1-(x-y))`，不是完整词表分布KL |

本项目已有[混批差分](../tests/model_mixed_batch_test.cpp)及
[普通/verify路径三角对照](../tests/mtp_path_triangle_test.cpp)。后续相关
改动可参考固定token前缀、改变批次组成/行位置/分块的对照方法；这只是
候选验证思路，不新增测试要求或自动执行队列。以上上游测试本次均未运行，
不把测试源码、deterministic开关或上游断言当作本项目已通过证据。

### 其余评估结论与适用边界

- DSpark动态验证长度可参考接受率与执行成本联合决策，但依赖训练好的
  Confidence Head；本项目现有MTP路径没有对应头。异步调度也需先取得
  本机CPU/GPU空转证据，再评估状态、取消和槽位复用的复杂度。
- SWA bounded replay包含近似窗口状态重建，不能用于恢复本项目依赖
  更早历史的DeltaNet完整状态；见[官方部署说明][deepinfer-qa]。
- [该版README][deepinfer-readme]列出DeepSeek-V4.1-Flash、SM100/Ascend
  支持及多卡性能图；不能外推到Thor SM110a、qwen4_exp或NVFP4专家。
- 已检查运行时未提供本项目所需的NVMe专家共享缓存或热启动直接修复。
  相关建议限于方法参考，不据此调整当前共享缓存/热启动工程决定。

[deepinfer-model]: https://github.com/deepseek-ai/DeepInfer/blob/774ce244b87cc0eb8d4b94ab9f5de2ab22049f01/deepinfer/model_executor/models/deepseek.py
[deepinfer-engram]: https://github.com/deepseek-ai/DeepInfer/blob/774ce244b87cc0eb8d4b94ab9f5de2ab22049f01/deepinfer/model_executor/models/v4_modules/engram.py
[deepinfer-userbuffer]: https://github.com/deepseek-ai/DeepInfer/blob/774ce244b87cc0eb8d4b94ab9f5de2ab22049f01/deepinfer/model_executor/parallel_utils/userbuffer.py
[deepinfer-batch-test]: https://github.com/deepseek-ai/DeepInfer/blob/774ce244b87cc0eb8d4b94ab9f5de2ab22049f01/tests/models/test_batch_invariant.py
[deepinfer-kl-test]: https://github.com/deepseek-ai/DeepInfer/blob/774ce244b87cc0eb8d4b94ab9f5de2ab22049f01/tests/models/test_kl.py
[deepinfer-kernel-config]: https://github.com/deepseek-ai/DeepInfer/blob/774ce244b87cc0eb8d4b94ab9f5de2ab22049f01/deepinfer/worker/runtime_config.py
[deepinfer-diff]: https://github.com/deepseek-ai/DeepInfer/blob/774ce244b87cc0eb8d4b94ab9f5de2ab22049f01/deepinfer/testing.py
[deepinfer-qa]: https://github.com/deepseek-ai/DeepInfer/blob/774ce244b87cc0eb8d4b94ab9f5de2ab22049f01/doc/inference_deployment_qa.md
[deepinfer-readme]: https://github.com/deepseek-ai/DeepInfer/blob/774ce244b87cc0eb8d4b94ab9f5de2ab22049f01/README.md
[deepinfer-ngram-meta]: https://github.com/deepseek-ai/DeepInfer/blob/774ce244b87cc0eb8d4b94ab9f5de2ab22049f01/deepinfer/model_executor/models/v4_modules/engram_metadata.py
[deepinfer-ngram-input]: https://github.com/deepseek-ai/DeepInfer/blob/774ce244b87cc0eb8d4b94ab9f5de2ab22049f01/csrc/engine/input_metadata/prepare_tensor_input_base.cpp
[deepinfer-zos-window]: https://github.com/deepseek-ai/DeepInfer/blob/774ce244b87cc0eb8d4b94ab9f5de2ab22049f01/deepinfer/model_executor/layers/tilelang_ops/cuda/gather_engram_tokens_zos.py
[deepinfer-scheduler]: https://github.com/deepseek-ai/DeepInfer/blob/774ce244b87cc0eb8d4b94ab9f5de2ab22049f01/csrc/engine/scheduler.hpp
[deepinfer-deepep-buffer]: https://github.com/deepseek-ai/DeepEP/blob/93eb6eb238127e96c6d7a4a625a6dad158348509/csrc/buffers/engram.hpp
[deepinfer-deepep-issue]: https://github.com/deepseek-ai/DeepEP/blob/93eb6eb238127e96c6d7a4a625a6dad158348509/deep_ep/include/deep_ep/impls/engram/engram_fetch.cuh
[deepinfer-deepep-wait]: https://github.com/deepseek-ai/DeepEP/blob/93eb6eb238127e96c6d7a4a625a6dad158348509/deep_ep/include/deep_ep/impls/engram/engram_fetch_wait.cuh
[deepinfer-tile-wheel]: https://files.pythonhosted.org/packages/89/64/9731e29c0b9d9c6a58a7e1f6353d4edbfc80591b9995f9026997fdab39c6/tile_kernels-2.1.0-py3-none-any.whl
[deepinfer-sampler]: https://github.com/deepseek-ai/DeepInfer/blob/774ce244b87cc0eb8d4b94ab9f5de2ab22049f01/deepinfer/model_executor/layers/sampler.py
[deepinfer-accept-kernel]: https://github.com/deepseek-ai/DeepInfer/blob/774ce244b87cc0eb8d4b94ab9f5de2ab22049f01/deepinfer/model_executor/layers/tilelang_ops/cuda/sampler.py

## Qwen3.8-Next 官方技术报告 (目标模型权威设计文档)

- 文件: `pdf/tech_report.pdf` (PDF 1.5, 2026-08-26, Qwen Team)
- 标题: "On the Design of Qwen3.8-Next Architecture: Evaluation, Efficiency,
  and Training Stability"
- 用途: **目标模型 Qwen3.8-Flash-Next 的官方架构设计文档**, 比 sglang/vllm
  源码更高层地解释设计动机与消融:
  - 125B 参数 / 6B 激活/token + 51B n-gram embedding 表 (off-accelerator,
    即 PLE SSD Stream 的 51.2 GB sidecar)。
  - Token mixing = Gated DeltaNet (GDN) + global attention 逐层混合, 每 4 层
    一个 full-attention; 继续预训练时 full-attention 被 QSA 替换 (压缩轻量
    indexer 在 micro-block 粒度打分) — 印证 36 linear + 12 full 的层布局。
  - 残差流展宽为 4 分支 + 逐元素门控 = **Gated Residual (GR)**, 即本项目的
    Hyper-Connection (hc_count=4)。
  - 容量经单个 n-gram embedding 层 (表从 host 内存预取) 加在主干外 — 即 PLE。
- 注意: 训练侧文档, 推理实现仍以 sglang-qwen4-exp / vllm 源码为准; 本文用于
  理解"为什么这样设计", 与 MODEL.md 的"是什么"互补。

## 明确不参考

- Qwen3x-Orin 的治理机制 (SDD / constitution / proof contract /
  evidence chain)。用户明确不采用。仅其 tokenizer 算法与 API 设计思路
  可借鉴。

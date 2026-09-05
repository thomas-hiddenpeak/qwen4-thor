# REFERENCE.md — 参考项目说明

> `reference/` 目录存放外部参考项目源码 (只读, 不参与构建,
> 不修改)。本文说明每个项目取什么、怎么用。

## 目录约定

```
reference/
├── sglang-ssd-stream/    # PLE SSD Stream 机制参考 (必读)
├── sglang-qwen4-exp/     # SGLang qwen4_exp.py 单文件 (PLE/forward 权威参考)
├── vllm/                 # vLLM main, 含完整 qwen4_exp 实现 (MTP/QSA/PLE 权威参考)
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
  kernel, 架构不同, 只取算法与张量布局语义。KV 方面本项目正从连续 KV
  迁移到 Paged KV (PD-ready 前提, 见 ARCHITECTURE.md "PD-ready 架构")。

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

## 明确不参考

- Qwen3x-Orin 的治理机制 (SDD / constitution / proof contract /
  evidence chain)。用户明确不采用。仅其 tokenizer 算法与 API 设计思路
  可借鉴。

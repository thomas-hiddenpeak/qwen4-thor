# REFERENCE.md — 参考项目说明

> `reference/` 目录存放外部参考项目源码 (只读, 不参与构建,
> 不修改)。本文说明每个项目取什么、怎么用。

## 目录约定

```
reference/
├── sglang-ssd-stream/    # PLE SSD Stream 机制参考 (必读)
├── qwen35-thor/          # 同硬件 Qwen3.5 引擎, 架构模式参考
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

## thor-probe / thor-bench (按需)

- 硬件探测方法与 Thor 性能基线数据 (FP4 GEMM 595 TFLOP/s 等)。
- Phase 3 性能调优时参考。

## 明确不参考

- Qwen3x-Orin 的治理机制 (SDD / constitution / proof contract /
  evidence chain)。用户明确不采用。仅其 API 设计思路可借鉴。

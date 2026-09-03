# STATUS.md — 当前状态快照

> 本文始终反映"现在"。历史状态见 [LOG.md](LOG.md)。

## 当前阶段

Phase 1 — 核心推理引擎 + PLE SSD Stream + HTTP API
(详见 [PHASES.md](PHASES.md))

## 已完成

- [x] 2026-09-03 项目初始化: git 仓库、目录骨架、文档体系、构建配置
- [x] 2026-09-03 模型下载完成 (140 GB, 含 51.2 GB PLE sidecar,
  SHA-256 待验证)
- [x] 2026-09-03 参考项目调研 (qwen35-thor / sglang-ssd-stream /
  thor-probe / thor-bench)

## 进行中

- (无)

## 阻塞 / 风险

- **PLE 查找机制未完全理解**: 每 token 16 个 row_id 的计算方式、
  PLE 在 forward pass 中的精确位置, 需从 sglang-ssd-stream 源码
  (reference/sglang-ssd-stream) 和 transformers 5.8 的 qwen4_exp
  实现中确认。
- **qwen4_exp 新字段语义待确认**: `hc_count`/`hc_lowrank`
  (hyper connection), `indexer_*`/`ngram_*` (ngram 索引),
  `heads_per_ngram`, `split_ngram_parts` 等。
- **PLE sidecar SHA-256 未验证** (ssd-stream.json 记录了期望值
  `b070f964...`, 51.2 GB 校验耗时较长, 安排在首次加载前完成)。

## 下一步

1. 克隆 sglang-ssd-stream 到 `reference/`, 研读 PLE SSD Stream 实现
   (io_uring 读取器、页去重、GPU 重叠、row_id 来源)。
2. 获取/研读 transformers 5.8 的 `qwen4_exp` 模型实现, 确认 forward
   pass 中 PLE 的调用位置与 row_id 计算。
3. 确认理解后, 更新 [MODEL.md](MODEL.md), 然后开始 Phase 1 实现。

## 环境

| 项 | 值 |
|---|---|
| 硬件 | Jetson AGX Thor, SM110a, 20 SM, 122 GB LPDDR5X |
| 驱动 / CUDA | 595.78 / 13.3 (nvcc 13.3.33) |
| CMake / GCC | 3.28.3 / 13.3.0 (aarch64) |
| 模型路径 | `~/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream` (只读) |
| 磁盘 | NVMe, 约 360 GB 可用 |

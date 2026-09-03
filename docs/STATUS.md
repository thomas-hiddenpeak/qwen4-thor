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
- [x] 2026-09-03 构建骨架验证通过: `q4t version` / `q4t probe` 可运行
  (probe 正确识别 Thor SM 11.0, 20 SM, 122.9 GB)
- [x] 2026-09-03 参考项目克隆到 reference/: sglang-ssd-stream
  (176a522, v0.2.0), qwen35-thor (57e2977)
- [x] 2026-09-03 GitHub 仓库创建并推送 (thomas-hiddenpeak/qwen4-thor)
- [x] 2026-09-03 liburing 2.5 安装 (PLE io_uring 依赖)

## 进行中

- Phase 1 实现:PLE 流式层。
  - ✅ ngram 哈希 (row_id 计算) 完成并通过测试:与 SGLang 参考逐位一致
    (含 EOS-ignoring 规则),multipliers 派生与 checkpoint 一致。
  - ⏳ io_uring SSD 读取器 (页去重 + 注册页池)。
  - ⏳ FP8→BF16 CUDA 转换 kernel。
  - ⏳ PLE 端到端 gather (对真实 51.2 GB 文件验证)。

## 阻塞 / 风险

- **PLE sidecar SHA-256 未验证** (ssd-stream.json 记录了期望值
  `b070f964...`, 51.2 GB 校验耗时较长, 安排在首次加载前完成)。
- **QSA 稀疏注意力细节**: indexer 的 top-k 选择算法在 SGLang 的
  `sglang/srt/layers/attention/qsa/` 模块 (尚未拉取), 实现
  full_attention 层前需研读。
- **DeltaNet SSM 细节**: linear_attention 继承 Qwen3.5 的
  GatedDeltaNet, 实现前需参考 qwen35-thor 的 deltanet 实现。

## 已解决 (2026-09-03)

- ✅ **PLE 查找机制完全理解**: PLE = n-gram 哈希查找表。
  每 token 取 [t-2,t-1,t] 3-gram 上下文, 16 个 head 各算一个
  哈希 row_id (splitmix 派生乘子 + 素数词表取模), 查 16 行
  (160B FP8) 拼成 2560 维嵌入, 经 key/value 投影 + 门控 +
  depthwise conv 后加到主干。详见 MODEL.md。
- ✅ **hc_*/indexer_*/ngram_* 字段语义确认**:
  hc_count=4 (hyper-connection 4 分支主干), indexer_* = QSA
  稀疏注意力索引器配置, ngram_* = PLE 查找参数。

## 下一步

1. 开始 Phase 1 实现。建议顺序:
   - **IO 层**: safetensors 解析 (mmap) + JSON 配置 + tokenizer
   - **量化层**: NVFP4 W4A4 / FP8 原语
   - **PLE 流式层**: ngram 哈希 (CPU/GPU) + io_uring 读取器 +
     页去重 + FP8→BF16 转换 (核心特性, 优先)
   - **模型层**: 48 层 forward (DeltaNet / QSA full-attn / MoE /
     hyper-connection / PLE 融合)
   - **引擎层**: 请求生命周期 + MTP + 采样
   - **服务层**: OpenAI 兼容 HTTP API
2. 实现 full_attention 前, 拉取 SGLang qsa 模块研读 indexer。
3. 实现 linear_attention 前, 研读 qwen35-thor 的 deltanet 实现。

## 环境

| 项 | 值 |
|---|---|
| 硬件 | Jetson AGX Thor, SM110a, 20 SM, 122 GB LPDDR5X |
| 驱动 / CUDA | 595.78 / 13.3 (nvcc 13.3.33) |
| CMake / GCC | 3.28.3 / 13.3.0 (aarch64) |
| 模型路径 | `~/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream` (只读) |
| 磁盘 | NVMe, 约 360 GB 可用 |

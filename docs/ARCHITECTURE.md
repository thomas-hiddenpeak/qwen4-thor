# ARCHITECTURE.md — 整体架构设计

> 设计意图文档。实现细节以代码为准。
> 状态: 初稿 (2026-09-03), 随实现推进修订。

## 设计原则

1. **专用 runner, 不做通用框架**。只为 Qwen3.8-Flash-Next 在
   Thor SM110a 上的部署做协同设计 (权重布局、状态所有权、
   调度、kernel、API 行为)。
2. **PLE SSD Stream 是一等公民**, 不是外挂优化。查找表的
   异步读取与 GPU 计算重叠是架构核心, 从第一天就按生产路径
   实现。
3. **以真实使用环境为准**: HTTP API + CLI 是开发和测试的
   主入口, 不依赖 Python 运行时。
4. **统一内存架构**: Thor 是 122 GB 统一内存, 不存在
   "H2D 拷贝" 的传统意义; 权重/状态/查找表都在同一地址空间,
   设计围绕带宽与重叠, 而非拷贝。

## 模块划分

```
q4t (单一可执行)
├── CLI 层          version / probe / models / generate / serve
├── 服务层          OpenAI 兼容 HTTP (chat/completions, streaming)
├── 引擎层          请求生命周期: tokenize → prefill → decode → 采样
│   ├── Runner      推理循环, 状态机 (prefill/decode 可分离路径,
│   │               见"PD-ready 架构")
│   ├── Scheduler   (Phase 2: 连续批处理; Phase 1: 单请求)
│   └── Sampler     greedy / temperature / top-p / top-k
├── 模型层          qwen4_exp forward pass
│   ├── 48 层混合注意力 (36 DeltaNet SSM + 12 GQA full)
│   ├── MoE (512 专家 top-10 + shared, NVFP4 grouped GEMM)
│   ├── PLE (layer 2, 16 次查找/token)
│   ├── MTP (1 层 draft, 推测解码)
│   └── Vision (27 层 ViT + merger)
├── PLE 流式层      ★ 核心
│   ├── RowPlanner  行 ID 计算 (16 行/token)
│   ├── PageDedup   4 KiB 页去重
│   ├── IouringReader  io_uring 并发读 (32 MiB 注册页池)
│   ├── Staging     2×16 MiB 固定暂存
│   └── ConvertStream  独立 CUDA stream 上 FP8→BF16
├── 状态层          Paged KV cache (✅ 已实现, PD-ready 前提),
│                   SSM state (FP32), 请求状态
├── 量化层          NVFP4 W4A4 / FP8 原语, 反量化
├── IO 层           safetensors (mmap 零拷贝), JSON, tokenizer
└── 核心层          设备探测, SHA-256, 日志
```

## 关键数据流 (decode 单 token)

```
token t 的 hidden state
  → [layer 2] PLE: 计算 16 个 row_id
       → RowPlanner 写入固定主机内存
       → PageDedup: 映射到 4 KiB 页, 去重
       → IouringReader: 提交 io_uring 读 (与 layer 0-1 的 GPU 计算重叠)
       → Staging: 行数据按序恢复
       → ConvertStream: FP8→BF16 (独立 CUDA stream)
       → 与主干融合 (融合方式待确认, 见 MODEL.md)
  → 其余层正常 forward (SSM / full attn / MoE)
  → lm_head → 采样 → token t+1
```

重叠目标: SSD 读取延迟 (NVMe 随机读 ~10-100 µs/页) 被
GPU 层计算掩盖; 仅当读取超过重叠窗口时才同步等待。

## 内存预算 (初估, 待实测)

| 项 | 估算 |
|---|---|
| 模型权重 (NVFP4 专家 + BF16 其余) | ~90 GB (safetensors 140 GB 含 PLE 51.2 GB; 专家打包后更小, 待实测) |
| PLE sidecar | 0 (走 SSD, 工作内存 75.17 MiB 实测 2026-09-07, < 100 MiB 预算) |
| KV cache (12 full-attn 层) | 按预算配置 |
| SSM state (36 层, FP32) | 按序列配置 |
| 激活 / 暂存 | 数百 MiB |
| **合计** | 需 < 122 GB, 留余量给 OS/CPU |

> [待确认] 精确权重占用需在首次加载时实测。若权重+状态
> 超预算, 参考 sglang-ssd-stream 的 grouped CPU-offload 策略
> (专家块在统一内存中按需预取)。

## 技术选型

| 项 | 选择 | 理由 |
|---|---|---|
| PLE I/O | 原生 Linux io_uring | sglang-ssd-stream 用 Rust io_uring; 我们 C++ 直接调用 liburing, 无跨语言边界 |
| GEMM | CUTLASS SM110 + 自研 GEMV | decode 单 token 走 GEMV, prefill 走 GEMM |
| HTTP | 轻量 C++ HTTP (自研或 cpp-httplib 级) | 无重依赖; OpenAI 兼容语义 |
| JSON | 有界解析器 (自研, 参考 Qwen3x-Orin 的 bounded JSON) | 拒绝无界分配 |
| Tokenizer | 自研 BPE (解析 tokenizer.json) | 无 Python 依赖 |

## PD-ready 架构 (Prefill/Decode 可分离)

> 用户决定 (2026-09-05): runner 后期有特殊场景需 PD 分离, 架构须
> 早期可分离, 避免后期返工。Phase 1 落地**架构可分离性**,
> 完整多设备 PD *部署*归 Phase 2。

**为什么 Paged KV 是 Phase 1 硬需求 (而非 Phase 2)**
PD 分离的价值来自 KV 在 prefill 与 decode 之间**迁移**。连续 KV
`[max_len, nkv, 2, hd]` 是按绝对位置寻址的整块, 无法按请求切分/迁移;
Paged KV (按页组织 + block table) 让 KV 成为可独立搬运的页序列,
是 KV 可迁移/共享/换页的硬前提。因此 Paged KV 随 PD-ready 提前到
Phase 1, 而非等到 Phase 2 连续批处理。

**Phase 1 落地的可分离性 (低成本、零风险)**
1. **prefill/decode 可分离代码路径**: prefill (T 首步) 与 decode (T=1)
   不融合, 各自可独立调用 (✅ 现状已满足, 保持)。
2. **Paged KV cache**: ✅ 已实现 (2026-09-05)。full_attention 按页组织
   (`kKvPageSize=16`) + 页表间接寻址, 替代连续 KV。恒等映射
   (`page_table[p]=p/16`) 下与旧布局逐位一致。见 PHASES.md 第 3/6 项。
3. **阶段边界 API**: ✅ 已实现 (2026-09-05)。`ModelSequence` 是轻量
   host-only 状态机 (stage: kIdle→kPrefill→kDecode, position, PLE
   history), 4 个操作: `ModelBeginSequence` (重置 per-layer 状态) →
   `ModelPrefill` (完成 prefill, **KV/SSM 状态就绪的交接点**) →
   `ModelDecodeStepSeq` (单 decode token, 自动维护 position/history) →
   `ModelEndSequence` (重置 kIdle)。使 runner 能把 prefill 与 decode
   驱动为两次独立调用。`ModelForward` 重构为 `ResetAllLayers +
   RunPrefill` (向后兼容)。测试 `model_sequence_api` 验证 prefill/decode
   与旧路径逐位一致。
4. **MTP 留在 decode 路径内** (draft 依赖 decode 的逐 token 流)。

**Phase 2 的完整 PD 部署 (依赖并发底座)**
多设备/多实例分离、KV 跨设备传输 (RDMA/NVLink/共享统一内存)、
独立 prefill/decode 调度池。单卡 Thor 无独立 prefill/decode 池可分,
完整 PD 分离的收益需多请求并发 (Phase 2 连续批处理) 才能体现。

## 与参考项目的关系

见 [REFERENCE.md](REFERENCE.md)。核心: 独立实现, 不 fork;
参考项目**灵活选用** (非锁定), 取其有用部分服务本项目:
qwen35-thor 的架构模式与 kernel 设计、sglang-ssd-stream 的 PLE 流式
机制、vLLM (`reference/vllm`) 的 MTP/QSA 语义。

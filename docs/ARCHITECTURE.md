# ARCHITECTURE.md — 整体架构设计

> 设计意图文档。实现细节以代码为准。
> 状态: 初稿 (2026-09-03), 随实现推进修订。
> 2026-09-20：评估规则见 [EVALUATION.md](EVALUATION.md)，实现进度见 STATUS.md。

## 设计原则

1. **专用 runner, 不做通用框架**。只为 Qwen3.8-Flash-Next 在
   Thor SM110a 上的部署做协同设计 (权重布局、状态所有权、
   调度、kernel、API 行为)。
2. **PLE SSD Stream 是一等公民**, 不是外挂优化。查找表的
   异步读取与 GPU 计算重叠是架构核心, 从第一天就按生产路径
   实现。
3. **以真实使用环境为准**：推理服务为原生 C++/CUDA；首项测试使用
   tools/evalscope 的 HTTP E2E，评估工具可使用 Python，不属于推理依赖。
4. **统一物理内存不等于零拷贝**：CPU/GPU 共享物理 DRAM，但代码仍有
   host/device 分配、cudaMemcpy 和同步。预算必须计入实际复制与缓存行为，
   不能从统一内存推导所有数据共享同一指针或复制无成本。

## 模块划分

```
q4t (单一可执行)
├── CLI 层          version / probe / models / generate / serve /
│                   bench-decode / bench-prefill
├── 服务层          OpenAI 兼容 HTTP (chat/completions, streaming)
├── 引擎层          请求生命周期: tokenize → prefill → decode → 采样
│   ├── Runner      推理循环, 状态机 (prefill/decode 可分离路径,
│   │               见"PD-ready 架构")
│   ├── Scheduler   ✅ 连续批处理 (token 级打包 + MTP lockstep,
│   │               独立调度线程合并并发请求)
│   └── Sampler     greedy (GPU argmax; 当前唯一采样模式)
├── 模型层          qwen4_exp forward pass
│   ├── 48 层混合注意力 (36 DeltaNet SSM + 12 GQA full)
│   ├── MoE (512 专家 top-10 + shared, NVFP4 grouped GEMM)
│   ├── PLE (0-indexed layer 1, 16 次查找/token)
│   ├── MTP (1 层 draft, 推测解码)
│   └── Vision (27 层 ViT + merger)
├── PLE 流式层      ★ 核心
│   ├── RowPlanner  行 ID 计算 (16 行/token)
│   ├── PageDedup   4 KiB 页去重
│   ├── IouringReader  io_uring 并发读 (32 MiB 注册页池)
│   ├── Staging     pinned host FP8 暂存 (capacity_tokens × 160 B)
│   └── ConvertStream  独立 CUDA stream 上 FP8→BF16
├── 状态层          Paged KV cache (✅ 已实现, PD-ready 前提),
│                   SSM state (FP32), 请求状态
├── 量化层          NVFP4 W4A4 / FP8 原语, 反量化
├── 运行时层        内存预算 (memory_budget: --mem-fraction + auto-length
│                   + 运行时 preflight 软降级, OOM 可靠性)
├── IO 层           safetensors (pread + hash 索引), JSON, tokenizer
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
       → 与主干融合 (PleLayerForward: key/value_proj + 门控 +
          depthwise conv, 加到 trunk, 见 MODEL.md PLE forward)
  → 其余层正常 forward (SSM / full attn / MoE)
  → lm_head → 采样 → token t+1
```

重叠目标: SSD 读取延迟 (NVMe 随机读 ~10-100 µs/页) 被
GPU 层计算掩盖; 仅当读取超过重叠窗口时才同步等待。

## 内存预算 (实测 2026-09-19)

| 项 | 实测 |
|---|---|
| 模型权重 (NVFP4 专家 + BF16 其余) | **84 GB** (WeightIndex::total_size() 精确) |
| 固定成本 (MTP/vision/scratch) | ~14.2 GB |
| PLE sidecar | 0 (走 SSD, 工作内存 75.17 MiB 实测 2026-09-07, < 100 MiB 预算) |
| full-attn KV+indexer (12 层) | 33356 B/token/seq (随 max_len×max_seq) |
| linear SSM state (36 层, FP32) | 110.4 MB/seq (O(1), 不随序列增长) |
| **合计** | `--mem-fraction` (默认 0.90) × MemTotal 预算; 启动前推导
  (max_len, max_seq) 上限, 装不下 CAPPED 回退, 任何配置不 OOM |

> 262144×seq1 实测峰值 ~96 GB (余 25.6 GB); 262144×seq4 OOM (~158 GB)。
> 详见 PHASES.md "长上下文 262K 内存预算" + STATUS.md OOM 可靠性条目。

## 技术选型

| 项 | 选择 | 理由 |
|---|---|---|
| PLE I/O | 原生 Linux io_uring | sglang-ssd-stream 用 Rust io_uring; 我们 C++ 直接调用 liburing, 无跨语言边界 |
| GEMM | cuBLASLt (BF16/FP4 W4A4) + 自研 GEMV | decode 单 token 走自研
  GEMV (warp-per-output + f32x2, 超 cuBLASLt); prefill 走 cuBLASLt; FP4
  W4A4 保留 nvjet tensor core (手写 SIMT 数学上无法超越) |
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

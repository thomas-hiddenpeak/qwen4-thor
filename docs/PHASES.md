# PHASES.md — 分阶段计划

> 范围与完成标准。当前阶段: **Phase 2** (Phase 1 已闭合 2026-09-07)。

## Phase 1 — 核心推理引擎 + PLE SSD Stream + HTTP API

**目标**: 在本机 Thor 上跑通 Qwen3.8-Flash-Next 的完整推理路径,
以真实使用环境 (HTTP API + CLI) 为准进行开发和测试。

### 范围

1. **构建与基础设施**
   - CMake 构建 (SM110a), 硬件探测 (`probe` 子命令)
   - safetensors 解析 (零拷贝 mmap), JSON 配置解析
   - NVFP4 W4A4 / FP8 量化原语
   - Tokenizer (BPE, 复用模型目录中的 tokenizer.json)

2. **PLE SSD Stream (核心特性)**
   - io_uring 异步读取器 (4 KiB 页粒度, 页去重)
   - 注册页池 + 固定暂存缓冲区
   - 独立 CUDA stream 上的 FP8→BF16 转换
   - 与 GPU 计算重叠, 仅在读取超过重叠窗口时同步
   - sidecar 完整性校验 (SHA-256, 启动时)

3. **模型 forward pass (text)**
   - 48 层: 36 linear_attention (DeltaNet SSM, FP32 state) +
     12 full_attention (GQA 24Q/2KV, head_dim 256, Paged KV)
     - **Paged KV cache 为 Phase 1 硬需求** (原为 Phase 2 目标, 因 PD-ready
       架构提前, 见第 6 项)。✅ 已实现 (2026-09-05): 按页组织
       (`kKvPageSize=16`) + 页表间接寻址, 使 KV 可按页迁移/共享。
   - MoE: 512 专家 top-10 + shared expert, NVFP4 grouped GEMM
   - PLE 嵌入 (0-indexed layer 1, 每 token 16 次查找)
   - MRoPE (interleaved, section [11,11,10], partial_rotary 0.25)
     - 纯文本 (t=h=w=position) 下退化为标准 partial RoPE, 与当前实现
       数学等价 (已验证); 完整 3D MRoPE (t/h/w 三行 + mrope_position_delta)
       已闭合 (2026-09-12, 多模态, 见 MODEL.md)。
   - MTP 推测解码 (1 层 full_attention draft)

4. **服务与 CLI**
   - `serve`: OpenAI 兼容 HTTP API (chat/completions, streaming,
     healthz), 以真实客户端 (curl) 为准测试
   - `generate`: 单次 greedy 生成
   - `version` / `probe` / `models`

5. **验证**
   - 验证标准的建设**单独讨论** (见 STATUS.md 阻塞项),
     初步方向: 与参考实现输出对比 (greedy, 逐 token)。
     参考实现**灵活选用** (非锁定): PLE 机制看 sglang-ssd-stream,
     MTP/QSA 语义看 vLLM (`reference/vllm`), MoE 量化看 qwen35-thor。
     逐 token 对比是初步方向, 正式验证标准体系见 Phase 2。

6. **PD-ready 架构 (Prefill/Decode 可分离, 设计目标)**
   - 背景: runner 后期有特殊场景需 PD 分离, 架构须早期可分离,
     避免后期返工 (用户决定, 2026-09-05)。
   - Phase 1 落地**架构可分离性** (低成本、零风险, 非完整多设备分离):
     - prefill (T 首步) 与 decode (T=1) 保持**可分离代码路径**, 不融合;
     - **Paged KV cache** (见第 3 项) 使 KV 可按页迁移/共享 — PD-ready 硬前提;
     - 暴露**阶段边界 API**: 引擎能"完成 prefill、交出 KV/SSM 状态"
       作为独立操作 (供 runner 驱动 prefill 与 decode 为两次调用);
     - MTP 留在 decode 路径内。
   - **完整 PD 分离部署** (多设备/多实例、KV 跨设备传输、独立调度池)
     归 **Phase 2** (依赖连续批处理 + 多请求调度, 单卡 Thor 无独立
     prefill/decode 池可分)。Phase 1 只保证架构不堵死该路径。

### 完成标准

- [x] `q4t serve` 在本机启动, 通过 OpenAI 兼容 API 完成多模态
      (文本 + 图像) 对话, 流式输出正常 (2026-09-07 图像接入, 见 LOG.md)
- [x] PLE SSD Stream 工作内存 < 100 MiB (不含模型权重),
      无 OOM, 无 swap (2026-09-07 实测 75.17 MiB, 见 LOG.md)
- [x] greedy 生成输出与参考实现一致 (2026-09-07 L2 噪声保真度验证,
      见 LOG.md; 标准: 置信位置 argmax 全对 + 翻转全 near-tie + l2_rel
      在 W4A4 噪声带, 无系统性错误)
- [x] MTP 推测解码可用 (2026-09-07, 见 LOG.md)
- [x] 多模态 (图像输入) 可用 (2026-09-07, 见 LOG.md)
- [x] Paged KV cache 落地 (full_attention 按页组织 + 页表间接寻址,
      替代连续 KV; 2026-09-05 完成, 见 LOG.md)
- [x] PD-ready 架构: prefill/decode 可分离路径 (✅ 现状已满足) +
      阶段边界 API (✅ ModelSequence, 2026-09-05 完成, 见 LOG.md;
      完整多设备 PD 部署归 Phase 2)

### 明确不做 (Phase 1 范围外)

- 连续批处理 / 多请求并发 (Phase 2)
- 完整多设备/多实例 PD 分离部署 (KV 跨设备传输、独立调度池;
  Phase 2, 依赖连续批处理。Phase 1 只做 PD-ready 架构, 见第 6 项)
- 性能调优 (kernel 融合、TMA、PDL 等, Phase 3)
- 视频输入 (✅ 2026-09-12 已闭合, 见下)

## Phase 2 — 多模态完善 + 并发

- [x] 视频输入 (temporal_patch_size=2) — **已闭合 (2026-09-12)**: 27 层
  ViT 逐时间组注意力 + `ProcessVideo` processor + serve 视频 part 接入 +
  混合图像/视频 batch, 见 STATUS.md / LOG.md 2026-09-11~12。
- [x] 连续批处理、多请求调度 — **已闭合 (2026-09-13~14)**: B1 多序列隔离
  + B2 连续批处理 (token 级打包 + serve 调度器) + MTP 批处理 Stage 1/2a/
  2b/2c + 调度 lockstep (计划 A), 见 STATUS.md。
- [x] 验证标准体系落地 — **已闭合 (2026-09-12)**: `tools/verify/` 三件套 +
  48 层全量基线 OVERALL PASS (置信位置 44/44 + 翻转全 near-tie + l2_rel
  噪声带), 见 STATUS.md。
- **长上下文 262K (262144) 验证** — 已闭合 (2026-09-15, 内存实测 +
  分块 prefill):
  `--max-len 262144 --max-seq 1` 加载成功, 峰值消耗 ~96 GB (余 25.6 GB,
  无 OOM), 短 prompt 生成正常且确定 — 与预算估算吻合 (见下)。**分块
  prefill 已实现**: `max_prefill` 语义从 prompt 上限改为分块大小 (上限 =
  `max_len`), `T > max_prefill` 走 chunk 0 `ModelPrefill` + chunk 1..
  `ModelDecodeBatch` 续块 (绝对位置, 不重置状态), 中间块跳过 lm_head,
  一次性 `[T, vocab]` logits 在 262K 下 = 130 GB 不可行的问题由此解决。
  200K-token prompt E2E 无 OOM 生成正常 (prefill ~12.6s/2048-token 块,
  262K ≈ 27 分钟, MoE GEMM 带宽下限)。
- **完整 PD 分离部署** — 降级为后续计划 (2026-09-14 用户决定): 当前
  PD-ready 架构 (Paged KV + 可分离路径 + `ModelSequence` 阶段边界 API)
  已满足**本机调度**需求 (持续 prefill 场景靠 Paged KV 按页迁移 + 独立
  调度池即可, 无需多设备); **多设备/双机扩展** (KV 跨设备传输) 归后续
  计划, 届时需多卡硬件验证。

### 长上下文 262K 内存预算 (2026-09-14 评估, 2026-09-15 实测确认)

关键架构事实: 36 层 linear attention 的 SSM state 是 **O(1) 不随序列增长**
(`ssm_state [max_seq, 48, 128, 128]`), 只有 **12 层 full attention 的
KV+indexer cache 随 max_len 线性增长**。运行时 ground truth: 模型加载后
GPU 进程占 **82.4 GB** (含 max_len=8192×max_seq=4 的 cache), GPU 总
122.9 GB。

max_len 8192 → 262144 的 cache 增量 (12 层 full attn, KV [max_seq,
n_pages, 16, 2, 2, 256] BF16 + idx_raw/comp [max_seq, max_len, 128] BF16 +
page_table + rope_pos):

| max_seq | cache 增量 | 总显存 (估) | 可行性 |
|---|---|---|---|
| 4 (当前) | ~78.4 GB | ~158 GB | **OOM, 不可行** (实测: 加载后统一内存耗尽, 机器崩机) |
| 1 | ~19.6 GB | ~101 GB | **可行** (2026-09-15 实测: 峰值消耗 ~96 GB, 余 25.6 GB, 无 OOM) |

另: prefill 工作区是独立约束 — 一次性 prefill 262K 的 `[T, vocab]`
logits = 130 GB 不可行, **必须分块 prefill** (✅ 已实现, 见上: `max_prefill`
改分块大小 + `ModelDecodeBatch` 续块, 不重置状态)。模型层硬伤 (如实报告):
QSA `idx_budget=2048` 在 262K 时 n_groups=65536, 只能 attend ~3% 历史块,
召回受限 — 模型设计问题, 非引擎问题。

## Phase 3 — 优化

- kernel 融合 (RMSNorm+GEMV, QKV merge, QK_norm+RoPE)
- TMA bulk copy / PDL (Programmatic Dependent Launch)
- MoE grouped GEMM 调优
- 性能基线建立 (参考 thor-bench 方法)

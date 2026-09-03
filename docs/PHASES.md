# PHASES.md — 分阶段计划

> 范围与完成标准。当前阶段: **Phase 1**。

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
   - MoE: 512 专家 top-10 + shared expert, NVFP4 grouped GEMM
   - PLE 嵌入 (layer 2, 每 token 16 次查找)
   - MRoPE (interleaved, section [11,11,10], partial_rotary 0.25)
   - MTP 推测解码 (1 层 full_attention draft)

4. **服务与 CLI**
   - `serve`: OpenAI 兼容 HTTP API (chat/completions, streaming,
     healthz), 以真实客户端 (curl) 为准测试
   - `generate`: 单次 greedy 生成
   - `version` / `probe` / `models`

5. **验证**
   - 验证标准的建设**单独讨论** (见 STATUS.md 阻塞项),
     初步方向: 与 sglang-ssd-stream 输出对比 (greedy, 逐 token)

### 完成标准

- [ ] `q4t serve` 在本机启动, 通过 OpenAI 兼容 API 完成多模态
      (文本) 对话, 流式输出正常
- [ ] PLE SSD Stream 工作内存 < 100 MiB (不含模型权重),
      无 OOM, 无 swap
- [ ] greedy 生成输出与参考实现 (sglang-ssd-stream) 一致
      (验证标准以单独讨论结论为准)
- [ ] MTP 推测解码可用
- [ ] 多模态 (图像输入) 可用

### 明确不做 (Phase 1 范围外)

- 连续批处理 / 多请求并发 (Phase 2)
- 性能调优 (kernel 融合、TMA、PDL 等, Phase 3)
- 视频输入 (图像先行, 视频 Phase 2)

## Phase 2 — 多模态完善 + 并发

- 视频输入 (temporal_patch_size=2)
- 连续批处理、多请求调度
- 长上下文 (262K) 验证
- 验证标准体系落地

## Phase 3 — 优化

- kernel 融合 (RMSNorm+GEMV, QKV merge, QK_norm+RoPE)
- TMA bulk copy / PDL (Programmatic Dependent Launch)
- MoE grouped GEMM 调优
- 性能基线建立 (参考 thor-bench 方法)

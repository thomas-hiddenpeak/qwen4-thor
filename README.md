# Qwen4-Thor

在 NVIDIA Jetson AGX Thor (SM110a, Blackwell) 上为
[Qwen3.8-Flash-Next](https://modelscope.cn/models/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream)
构建的原生 C++23 (host + device, CUDA 13.3) 推理引擎。

核心特性:**PLE SSD Stream** —— 51.2 GB FP8 n-gram 哈希嵌入查找表 (PLE,
技术报告称 N-gram Embedding Layer, 仅 Layer 2 一层) 不驻留内存,通过
Linux `io_uring` 从本地 NVMe 异步流式读取,与 GPU 计算重叠,释放约
47.6 GiB 统一内存。

## 能力概览

- **文本推理**: 48 层完整 forward (36 linear_attention + 12 full_attention),
  NVFP4 W4A4 量化, Paged KV cache, greedy 生成
- **多模态**: 27 层 ViT, 图像 + 视频输入 (OpenAI `image_url` / `video_frames`
  base64 接入), 3D MRoPE
- **MTP 推测解码**: 1 层 draft + 主模型验证 + 接受/回退; 多序列批处理
  (调度 lockstep) + chunked MTP 长上下文投机解码 (44K 上下文精度无损)
- **连续批处理**: token 级打包, 多请求并发调度, 无跨序列污染
- **长上下文**: 262K (`--max-len 262144 --max-seq 1` 实测可行), 流式 top-k
  QSA 召回 (移除 8192 硬上限)
- **OOM 可靠性**: 启动内存预算 (`--mem-fraction`, 默认 0.90) + auto-length
  推导 + 运行时 preflight 软降级, 任何配置不 OOM
- **OpenAI 兼容 API**: chat/completions (流式/非流式) + healthz
- **验证**: 76 项测试全绿零警告; `tools/verify/` 三件套对
  transformers 5.16.1 参考 48 层全量基线 OVERALL PASS

## 目标模型

- 架构: `qwen4_exp` (48 层: 36 linear_attention + 12 full_attention)
- MoE: 512 专家/层, top-10 路由 + shared expert, NVFP4 W4A4 量化
- 注意力: GQA 24Q/2KV, head_dim 256, MRoPE (interleaved)
- PLE: 每 token 16 次确定性查找, FP8 行 160 字节, 51.2 GB sidecar
- MTP: 1 层 full_attention 推测解码
- 视觉: 27 层 ViT (图像/视频输入)
- 上下文: 262K

## 硬件目标

| 项 | 值 |
|---|---|
| GPU | NVIDIA Thor, SM110a, 20 SM |
| 内存 | 122 GB LPDDR5X 统一内存 |
| CPU | 14 核 ARM Neoverse V3AE |
| 存储 | NVMe SSD |
| 工具链 | CUDA 13.3, CMake 4.0+ (pip 装 4.4.3), g++-14 (apt) (aarch64) |

## 构建

```bash
# 要求: CMake >= 4.0 (CMAKE_CUDA_STANDARD 23 仅在 4.x 映射 nvcc --std=c++23)
#   pip install --user --break-system-packages cmake==4.4.3
# 要求: g++-14 (nvcc 的 --std=c++23 需 host 编译器支持, GCC 13 会静默忽略)
#   sudo apt-get install g++-14
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_CXX_COMPILER=g++-14 -DCMAKE_CUDA_HOST_COMPILER=g++-14 \
  -DQ4T_CUDA_ARCHITECTURES=110a
cmake --build build --parallel

# 测试 (76 项)
ctest --test-dir build --output-on-failure
```

## 使用

```bash
# 版本与硬件探测
./build/q4t version
./build/q4t probe

# 单次生成 (greedy; --mtp 开启推测解码, --mtp-k 设 draft 步数, 默认 3)
./build/q4t generate "用一句话解释统一内存。" \
  [--model-dir DIR] [--max-tokens 32] [--max-prefill N] [--mtp] [--mtp-k 3]

# OpenAI 兼容 API 服务
./build/q4t serve \
  [--model-dir DIR] [--port 8080] \
  [--max-len N] [--max-seq N] [--max-prefill N] [--max-tokens N] \
  [--no-mtp] [--mem-fraction 0.90] [--no-budget]

# 吞吐基准
./build/q4t bench-decode [--batch B] [--steps N] [--prompt P] [--max-len L]
./build/q4t bench-prefill [--batch B] [--prompt P] [--sweep]
```

serve 请求体支持 OpenAI chat/completions 格式, content 可为字符串或
parts 数组; 图像用 `image_url` (base64 data URL), 视频用 `video_frames`
(base64 帧数组)。

**长上下文注意**: `--max-len` 按需最小 (如 44K 用 49152), 单请求场景
显式 `--max-seq 1`; 未 pin `--max-len` 时按 `--mem-fraction` 自动推导
(auto-length), 装不下的配置会 CAPPED 回退而非 OOM。

## 文档

- [AGENTS.md](AGENTS.md) — 开发入口, 面向 agent 的项目导航
- [docs/](docs/README.md) — 开发记录文档体系 (状态、架构、阶段计划、日志)
- [reference/](reference/) — 外部参考项目 (只读, 不参与构建)

## 参考项目

| 项目 | 用途 |
|---|---|
| [qwen35-thor](https://github.com/thomas-hiddenpeak/qwen35-thor) | 同硬件 Qwen3.5 推理引擎, 架构模式参考 |
| [sglang-ssd-stream](https://github.com/garnermccloud/sglang-ssd-stream) | PLE SSD Stream 机制参考 |
| [vllm](https://github.com/vllm-project/vllm) | qwen4_exp 完整实现, MTP/QSA 权威参考 |
| [thor-probe](https://github.com/thomas-hiddenpeak/thor-probe) | 硬件探测方法 |
| [thor-bench](https://github.com/thomas-hiddenpeak/thor-bench) | 硬件性能基线 |

## 许可证

MIT (项目代码)。模型权重遵循其发布方条款。

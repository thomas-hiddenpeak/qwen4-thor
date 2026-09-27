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
  base64 接入), 3D MRoPE；候选默认拒绝媒体，须显式
  `serve --allow-media`进入未验收实验路径（部署身份见STATUS）
- **MTP 推测解码**: draft、主模型验证与多序列调度已实现；默认关闭，
  不属于当前文本基线的完整验收范围
- **连续批处理**: token 级打包、多请求调度；已完成有界槽位和生命周期
  验证，不能据此保证所有路径均不存在状态污染
- **长上下文**: 流式 top-k QSA；当前正式单流评估覆盖 1K、4K、8K、44K、
  200K，历史 262K 运行记录不代替当前发布验收
- **资源管理**: 启动内存预算、auto-length 和运行时 preflight；不保证
  任意配置或异常输入不 OOM，媒体解压等预算仍待完善
- **HTTP API**: OpenAI 风格 chat/completions 流式/非流式接口，以及模型、
  健康、指标和取消接口；仅 greedy，不代表支持全部 OpenAI 参数语义
- **验证边界**: 已有数值缺陷修复、状态/故障测试和五档 HTTP 性能参考。
  尚未完成商用发布验收；当前结论见 [STATUS](docs/STATUS.md)，
  已知缺陷与未覆盖项见 [完善度审计](docs/PROJECT_READINESS_REVIEW_2026-09-27.md)

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

# 开发测试（可能有跳过项；退出成功不等于发布验收通过）
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
  [--model-dir DIR] [--host 127.0.0.1] [--port 8080] \
  [--max-len N] [--max-seq N] [--max-prefill N] [--max-tokens N] \
  [--mtp] [--allow-media] [--mem-fraction 0.90]

```

serve 默认监听 127.0.0.1，MTP 默认关闭。性能评估使用
[tools/evalscope](tools/evalscope/README.md) 的真实 HTTP E2E；bench 不作为
性能验收依据。Bug 修复按 [EVALUATION](docs/EVALUATION.md) 选择直接测试。

serve 请求体使用 OpenAI chat/completions 风格, content 可为字符串或
parts 数组; 图像用 `image_url` (base64 data URL), 视频用 `video_frames`
(base64 帧数组)。

**长上下文注意**: `--max-len` 按需最小 (如 44K 用 49152), 单请求场景
显式 `--max-seq 1`; 未 pin `--max-len` 时按 `--mem-fraction` 自动推导
(auto-length)。预算器可对部分超预算配置执行 CAPPED 回退；这不覆盖
系统其他进程、运行时异常输入或全部主机内存分配。

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

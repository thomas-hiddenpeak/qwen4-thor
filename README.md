# Qwen4-Thor

在 NVIDIA Jetson AGX Thor (SM110a, Blackwell) 上为
[Qwen3.8-Flash-Next](https://modelscope.cn/models/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream)
构建的原生 C++17/CUDA 推理引擎。

核心特性:**PLE SSD Stream** —— 51.2 GB FP8 逐层嵌入查找表 (Per-Layer
Embedding) 不驻留内存,通过 Linux `io_uring` 从本地 NVMe 异步流式读取,
与 GPU 计算重叠,释放约 47.6 GiB 统一内存。

## 目标模型

- 架构: `qwen4_exp`(48 层: 36 linear_attention + 12 full_attention)
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
| 工具链 | CUDA 13.x, CMake 3.24+, GCC 13+ (aarch64) |

## 构建

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DQ4T_CUDA_ARCHITECTURES=110a
cmake --build build --parallel
```

## 使用

```bash
# 版本与硬件探测
./build/q4t version
./build/q4t probe

# 单次生成 (greedy)
./build/q4t generate /path/to/model \
  --prompt "用一句话解释统一内存。" --max-tokens 32

# OpenAI 兼容 API 服务
./build/q4t serve /path/to/model --host 127.0.0.1 --port 8080
```

## 文档

- [AGENTS.md](AGENTS.md) — 开发入口, 面向 agent 的项目导航
- [docs/](docs/README.md) — 开发记录文档体系 (状态、架构、阶段计划、日志)
- [reference/](reference/) — 外部参考项目 (只读, 不参与构建)

## 参考项目

| 项目 | 用途 |
|---|---|
| [qwen35-thor](https://github.com/thomas-hiddenpeak/qwen35-thor) | 同硬件 Qwen3.5 推理引擎, 架构模式参考 |
| [sglang-ssd-stream](https://github.com/garnermccloud/sglang-ssd-stream) | PLE SSD Stream 机制参考 |
| [thor-probe](https://github.com/thomas-hiddenpeak/thor-probe) | 硬件探测方法 |
| [thor-bench](https://github.com/thomas-hiddenpeak/thor-bench) | 硬件性能基线 |

## 许可证

MIT (项目代码)。模型权重遵循其发布方条款。

# Qwen4-Thor

在 NVIDIA Jetson AGX Thor (SM110a, Blackwell) 上为
[Qwen3.8-Flash-Next](https://modelscope.cn/models/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream)
构建的原生 C++23 (host + device, CUDA 13.3) 推理引擎。

核心特性:**PLE SSD Stream** —— 51.2 GB FP8 n-gram 哈希嵌入查找表 (PLE,
技术报告称 N-gram Embedding Layer, 仅 Layer 2 一层) 不驻留内存,通过
Linux `io_uring` 按需读取本地 NVMe 页面，避免常驻约 47.68 GiB 的整表。
仍需页池、pinned staging 和 GPU scratch；读取是否被已提交 GPU 工作掩盖
取决于实际调度，不保证固定重叠比例。

## 能力概览

- **文本推理**: 48 层完整 forward (36 linear_attention + 12 full_attention),
  MoE 专家使用 NVFP4 W4A4，其他路径含 BF16/FP32；Paged KV cache、greedy 生成
- **多模态**: 27 层 ViT, 图像 + 视频输入 (OpenAI `image_url` / `video_frames`
  base64 接入), 3D MRoPE；当前版本默认拒绝媒体，须显式
  `serve --allow-media`进入未验收实验路径（部署身份见STATUS）
- **MTP 推测解码**: 默认开启 T>1 batch verifier（k=3），`--no-mtp` 回退；
  当前单流近 tie 合同、质量与性能验收状态见 STATUS
- **专家按需驻留**: 实现了可选 MoE 分层驻留；默认全部专家驻留，
  `--moe-resident-slots` 显式启用按需读取，不与 PLE 整表流式读取混为一谈；
  offload 研究结果不等于当前默认 MTP 基线验收
- **连续批处理**: token 级打包、多请求调度；已完成有界槽位和生命周期
  验证，不能据此保证所有路径均不存在状态污染
- **长上下文**: 流式 top-k QSA；当前正式单流评估覆盖 1K、4K、8K、44K、
  200K，历史 262K 运行记录不代替当前发布验收
- **资源管理**: 启动内存预算、auto-length、运行时 preflight，以及由
  配置/张量形状推导的设备预留与所有权核对；受测热缓存启动已完成
  [有界可靠性交付](docs/STARTUP_DELIVERY_2026-10-21.md)。不保证任意
  配置或异常输入不 OOM，媒体解压等预算仍待完善
- **HTTP API**: OpenAI 风格 chat/completions 流式/非流式接口，以及模型、
  健康、指标和取消接口；仅 greedy，不代表支持全部 OpenAI 参数语义
- **验证边界**: 已有数值缺陷修复、状态/故障测试和五档 HTTP 性能参考。
  [首版私有文本部署](docs/RELEASE_TEXT_V1_2026-09-28.md)已验收；
  公网多租户与业务SLA另行验收。当前结论见 [STATUS](docs/STATUS.md)，
  已知缺陷与未覆盖项见 [完善度审计](docs/PROJECT_READINESS_REVIEW_2026-09-27.md)

## 目标模型

- 架构: `qwen4_exp` (48 层: 36 linear_attention + 12 full_attention)
- MoE: 512 专家/层, top-10 路由 + shared expert, NVFP4 W4A4 量化
- 注意力: GQA 24Q/2KV, head_dim 256, MRoPE (interleaved)
- PLE: 每 token 16 次确定性查找, FP8 行 160 字节, 51.2 GB sidecar
- MTP: 1 层 full_attention 推测解码
- 视觉: 27 层 ViT (图像/视频输入)
- 模型配置最大上下文: 262144 token；当前正式 HTTP 矩阵最大名义档为 204800

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
#   sudo apt-get install g++-14 pkg-config liburing-dev libicu-dev
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_CXX_COMPILER=g++-14 -DCMAKE_CUDA_HOST_COMPILER=g++-14 \
  -DQ4T_CUDA_ARCHITECTURES=110a
cmake --build build --parallel

# 开发测试（默认SKIP也非零退出；发布范围与入口见tools/release）
python3 tools/evalscope/gpu_exclusive.py \
  --output .q4t-work/manual-tests/lease -- \
  ctest --test-dir build --output-on-failure
```

## 使用

```bash
# 版本与硬件探测
./build/q4t version
./build/q4t probe

# 独立 CLI 生成路径；不属于下述 HTTP MTP 验收范围
./build/q4t generate "用一句话解释统一内存。" --max-tokens 32

# 已验收的 HTTP 单流配置；默认 MTP k=3
./build/q4t serve --host 127.0.0.1 --port 8000 \
  --max-seq 1 --max-prefill 8192 --max-len 208896
# 普通 decode 回退：在上述 serve 命令末尾添加 --no-mtp
```

`serve` 参数默认值来自 `ServerOptions`，不是上述示例的显式配置：

| 参数 | 默认值 / 行为 |
|---|---|
| `--host` / `--port` | `127.0.0.1` / `8000`；host 接受数字 IPv4 |
| `--max-seq` | `1` |
| `--max-prefill` | `0` 表示使用模型默认 `2048`；可显式设至 `8192` |
| `--max-len` | `0`，默认预算开启时自动推导有效长度 |
| `--max-tokens` | 请求省略输出上限时使用 `256` |
| `--mtp` / `--no-mtp` / `--mtp-k` | MTP 默认开，k 默认 `3`；serve 接受 k=1..16，验收仅覆盖 k=3 |
| `--allow-media` | 默认不启用，显式启用实验媒体路径 |
| `--mem-fraction` | `0.90`，启动内存预算比例 |
| `--moe-resident-slots` | `0`，所有专家驻留；非零启用按需驻留 |
| `--startup-plan` | 默认`auto`；`off`保留旧分配路径，`required`要求支持并启用预留计划 |

`--model-dir DIR` 可覆盖代码中的本机默认模型路径。完整参数解析见
[src/server/server_options.cpp](src/server/server_options.cpp)。当前 main 使用
T>1 batch verifier；`--mtp-verifier` 已移除，sequential 实现在历史提交
`7f6a69c` 中保留。当前交付与基线见 [STATUS](docs/STATUS.md)。

启动计划在`auto`下仅对明确不支持的模式在计划开始前选择旧路径；
元数据、预留或交付失败直接停止。支持范围与本次1K可靠性验证见
[正式交付记录](docs/STARTUP_DELIVERY_2026-10-21.md)，不代表新增长输入、
并发或性能优化结论。

`generate` 是另一套调用路径：输出上限默认 64，默认尝试加载 MTP，
接受 `--mtp`、`--mtp-k`、`--max-prefill`、`--model-dir`、`--max-tokens`，
**目前不接受 `--no-mtp`**；不能将 serve 的参数或验收直接套用到它。

冻结验收范围为本机 Thor、既定 checkpoint、纯文本 greedy、单流 k=3、
`--max-seq 1 --max-prefill 8192 --max-len 208896`。其他 CLI、媒体、并发
或 k 值不随此矩阵取得验收。T>1 与普通 decode 的舍入路径不同，遵守
[EVALUATION](docs/EVALUATION.md) 的 N=4 ULP 近 tie 合同；不承诺跨路径
逐位一致。默认监听 127.0.0.1。

性能评估使用 [tools/evalscope](tools/evalscope/README.md) 的真实 HTTP E2E；
bench 不作为性能验收依据。归并方案、共用正确性修复与回退点见
[MTP 归并边界](docs/MTP_MERGE_BOUNDARY_2026-10-15.md)。

`POST /v1/chat/completions` 使用 OpenAI 风格请求体，content 可为字符串或
parts 数组; 图像用 `image_url` (base64 data URL), 视频用 `video_frames`
(base64 帧数组)，仅在 `--allow-media` 下可接入。另有 `GET /healthz`、
`GET /v1/models`、`GET /metrics` 与 `POST /v1/requests/cancel`。

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

# AGENTS.md — Qwen4-Thor 开发入口

任何 agent 进入本项目, 先读本文, 再按需要读 docs/ 下对应文档。
**代码是最高事实来源**; 文档与代码不一致时, 以代码为准并修正文档。

## 项目一句话

在 Jetson AGX Thor (SM110a) 上用 C++17/CUDA 实现
Qwen3.8-Flash-Next (qwen4_exp) 的原生推理引擎,
核心特性是 PLE SSD Stream (51.2 GB FP8 查找表从 NVMe 异步流式读取)。

## 文档导航

| 想了解什么 | 读哪个文档 |
|---|---|
| 当前进展到哪了、下一步做什么 | [docs/STATUS.md](docs/STATUS.md) |
| 整体架构设计 | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| 分阶段计划与范围 | [docs/PHASES.md](docs/PHASES.md) |
| 开发日志 (按时间倒序) | [docs/LOG.md](docs/LOG.md) |
| 目标模型架构细节 (qwen4_exp) | [docs/MODEL.md](docs/MODEL.md) |
| 参考项目说明 | [docs/REFERENCE.md](docs/REFERENCE.md) |

## 硬性约定

1. **模型目录只读**。模型位于
   `~/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream`,
   绝不写入、移动、修改。
2. **构建产物只进 `build/` 或 `.q4t-work/`** (已被 .gitignore 排除)。
3. **reference/ 目录只读**, 存放外部参考项目源码, 不参与构建,
   不修改其内容。
4. **代码风格**: Google C++ Style, 2 空格缩进, 80 列, 指针靠左
   (见 .clang-format)。命名: PascalCase 类型/方法, lower_snake_case
   局部变量, 成员变量尾下划线 (`member_`), I 前缀接口。
5. **编译零警告**: `-Wall -Wextra`。
6. **每次有意义的改动后**, 更新 `docs/STATUS.md` 并在 `docs/LOG.md`
   顶部追加一条记录 (日期、做了什么、为什么、下一步)。
7. **验证以真实环境为准**: 开发过程直接在本机 Thor 上构建、运行、
   测试, 不假设 CI 环境。

## 快速上手

```bash
# 构建
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DQ4T_CUDA_ARCHITECTURES=110a
cmake --build build --parallel

# 运行
./build/q4t version
./build/q4t probe
```

## 当前状态速览

> 详细状态见 [docs/STATUS.md](docs/STATUS.md)

- 阶段: Phase 1 (核心推理引擎 + PLE SSD Stream + HTTP API)
- 模型下载: 已完成 (140 GB, 含 51.2 GB PLE sidecar)
- 已完成: PLE 流式层 (核心特性) / IO 层 (JSON/safetensors/config/权重/
  tokenizer) / 量化层 (NVFP4 W4A4 全套) / 模型层 (48 层完整 forward +
  PLE 注入 + head/tail + generate + 长序列 QSA 稀疏路径) / serve (OpenAI
  兼容 HTTP API) / **PD-ready 架构** (Paged KV cache + 可分离代码路径 +
  阶段边界 API ModelSequence) / **4 层参考验证** (transformers 5.16.1
  官方实现, 含首个 full_attention, 4/4 argmax 匹配) / **decode 路径
  自洽性验证** (prefill/decode 同 token 对照, SSM state 改 FP32,
  conv1d 窗口 + PLE short-conv 持久状态两个 decode bug 已修复,
  C++ batch vs incremental 逐位一致 / 参考 4/4 argmax 匹配)。
  54 项测试全绿, 零警告。
- 下一步 (2026-09-06 与用户确认的顺序): 逐 token 对参考验证 (4 层基线
  + decode 自洽性已完成, 继续扩展到 decode 对参考逐步对照/更多层) →
  prefill/decode 性能优化 (拆分 linear attention 路径, 预留 MTP 接口)
  → MTP 推测解码 (良好基线上, 用户排期) → 多模态图像输入 (transformers
  权威参考已就位) → PLE 工作内存/SHA-256 校验。完整多设备 PD 部署归
  Phase 2。

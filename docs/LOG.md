# LOG.md — 开发日志

> 按时间倒序, **只追加不修改**。每条: 日期、做了什么、为什么、
> 下一步。发现历史错误时追加更正条目, 不改原文。

---

## 2026-09-03 — 骨架落地: 构建验证 + 参考克隆 + 推送 GitHub

**做了什么**
- 建立完整目录骨架 (include/q4t, src/{core,io,quant,text,model,
  runtime,kernels,ple,vision,server,mtp}, tests, tools, reference)。
- CMake 构建骨架: C++17/CUDA (SM110a), `-Wall -Wextra`,
  `find_package(CUDAToolkit)`, liburing 检测 (pkg-config)。
  产物 `build/q4t`。
- `q4t version` / `q4t probe` 实现并验证: probe 正确识别
  NVIDIA Thor (CC 11.0, 20 SM, 122.9 GB, L2 32 MB, 228 KB smem/SM,
  1048 MHz)。`generate` / `serve` / `models` 为 stub (exit 2)。
- 克隆参考项目到 reference/ (gitignore, 只读):
  - sglang-ssd-stream @ 176a522 (v0.2.0)
  - qwen35-thor @ 57e2977 (不含 submodule)
- 安装 liburing 2.5 (liburing-dev, apt)。
- git init (main) + 首次提交 + 推送
  github.com/thomas-hiddenpeak/qwen4-thor (private)。

**踩坑记录**
- 终端会话 cwd 会被重置回 /home/rm01/Orator, `cd` 不可靠;
  一律使用绝对路径 (`cmake -S <abs> -B <abs>`, `git -C <abs>`)。
- CUDA 13 移除了 `cudaDeviceProp::clockRate` / `maxClockRate`,
  改用 `cudaDeviceGetAttribute(cudaDevAttrClockRate)`。

**下一步**
- 研读 reference/sglang-ssd-stream (PLE 机制) 与 transformers 5.8
  的 qwen4_exp 实现, 消解 MODEL.md 中的 [待确认] 项。

---

## 2026-09-03 — 项目初始化

**做了什么**
- 完成模型与四个参考项目的调研 (qwen35-thor / Qwen3x-Orin /
  thor-probe / thor-bench / sglang-ssd-stream)。
- 确认目标: 完全独立的新项目, C++17/CUDA, 第一版即含多模态,
  PLE SSD Stream 为核心特性, 直接自研 (不依赖 sglang-ssd-stream
  运行时), HTTP API 纳入 Phase 1, 以真实使用环境为准开发测试。
- 确认环境: Jetson AGX Thor (SM110a), 122 GB 统一内存,
  CUDA 13.3, CMake 3.28, GCC 13.3 (aarch64)。
- 模型下载完成: 140 GB, 含 51.2 GB PLE sidecar
  (`ple/qwen3.8-flash-next-ple-fp8.bin`)。
- 建立仓库骨架: .gitignore / .clang-format / LICENSE (MIT) /
  README / AGENTS.md / docs 文档体系 / CMake 构建骨架。

**关键决策**
- 不采用 Qwen3x-Orin 的 SDD/constitution/proof contract 治理
  机制 (用户明确不喜欢, 过度强调证据、忽略工程本质)。
  改用轻量文档体系: STATUS (快照) + LOG (追加式日志) +
  ARCHITECTURE + PHASES + MODEL + REFERENCE。
- 验证标准 (正确性如何判定) **暂不定义**, 后续单独讨论;
  初步方向是与 sglang-ssd-stream 的 greedy 输出对比。
- 参考项目源码放 `reference/` (只读, 不参与构建), 便于随时
  查阅实现细节。
- 代码风格沿用 Orator 的 Google C++ Style 约定
  (2 空格 / 80 列 / 指针靠左 / member_ 尾下划线 / I 前缀接口)。

**模型架构要点 (来自 config.json, 详见 MODEL.md)**
- `qwen4_exp`: 48 层 (36 linear_attn + 12 full_attn, 每 4 层一个
  full), hidden 2560, MoE 512 专家 top-10 + shared expert
  (intermediate 640), 路由专家 NVFP4 W4A4, 其余 BF16。
- PLE: 51.2 GB FP8 查找表 (320,001,536 行 × 160 字节),
  sidecar 文件, 每 token 16 次确定性查找。
- MTP: 1 层 full_attention, hybrid=true。
- Vision: 27 层 ViT, hidden 1152, patch 16, temporal_patch 2。
- 上下文 262K, MRoPE interleaved section [11,11,10]。

**下一步**
1. 克隆 sglang-ssd-stream 到 `reference/`, 研读 PLE SSD Stream
   实现与 row_id 计算。
2. 研读 transformers 5.8 的 qwen4_exp 实现, 确认 forward pass
   中 PLE 位置。
3. 更新 MODEL.md, 开始 Phase 1 实现。

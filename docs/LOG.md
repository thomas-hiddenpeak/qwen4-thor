# LOG.md — 开发日志

> 按时间倒序, **只追加不修改**。每条: 日期、做了什么、为什么、
> 下一步。发现历史错误时追加更正条目, 不改原文。

---

## 2026-09-03 — IO 层: config.json 解析 (ModelConfig)

**做了什么**
- 实现 `include/q4t/io/model_config.h` + `src/io/model_config.cpp`:
  把 config.json 的 text_config 超参解析到 `ModelConfig` 结构体, 含
  RopeParams / MtpConfig / QuantConfig 子结构, 以及派生方法
  (num_full_attention_layers / IsFullAttention)。
- 覆盖字段: 核心维度 (48 层 / hidden 2560 / vocab 248320 / 262K ctx)、
  full attention (24 头 / 2 KV / head_dim 256 / interval 4)、MoE
  (512 专家 / top-10 / inter 640)、linear attention (DeltaNet 头/维/conv)、
  QSA indexer (budget 2048 / compress 4)、PLE (ngram 3 / 8 头 / embed
  2560 / layer_ids [2] / vocab base 2e7)、hyper-connection (hc 4 / lowrank
  320)、MRoPE (section [11,11,10] / partial 0.25 / theta 1e7)、MTP (1 层
  full_attention)、NVFP4 量化 (ignore 列表)、token ids。
- 不变量校验: model_type 必须 qwen4_exp、layer_types 长度 == 层数、
  至少 1 个 full_attention、ple_embed_dim 是 ngram_heads 的倍数。
- 测试 `tests/io_model_config_test.cpp`: 2 项 (真实 config.json 全字段
  断言; 缺失文件报错)。共 19 项测试全绿。

**下一步**
- IO 层续: tokenizer (tokenizer.json 解码) + 权重加载编排。

---

## 2026-09-03 — IO 层核心: JSON 解析器 + safetensors mmap 读取器

**做了什么**
- 实现 `include/q4t/io/json.h` + `src/io/json.cpp`: 最小递归下降 JSON
  解析器 (对象/数组/字符串含 \u 转义与 surrogate pair/数字/true/false/
  null), 产出小型 Json 值类型 (带 GetInt/GetString/GetArray 等访问器),
  错误带偏移定位。无外部依赖, 复用于所有模型 JSON 文件。
- 实现 `include/q4t/io/safetensors.h` + `src/io/safetensors.cpp`:
  mmap 只读 safetensors 读取器。解析 8 字节头长 + JSON 头 + 数据区,
  提取每张量 dtype/shape/data_offsets; 按需读字节 (ReadTensor) 或
  H2D (ReadTensorToDevice)。Dtype 支持 F64/F32/F16/BF16/I64..I8/U8/
  BOOL/F8_E4M3/F8_E5M2。
- CMake: 独立 `q4t_io` 静态库 (json.cpp + safetensors.cpp, 链接
  CUDA::cudart)。
- 测试: `tests/io_json_test.cpp` (4 项: 标量/嵌套数组/字符串转义/错误
  定位) + `tests/io_safetensors_test.cpp` (2 项: 合成文件解析+读取;
  真实模型 scale 文件头解析)。共 17 项测试全绿。

**踩坑**
- C++ raw string 定界符 `R"json(...)json"` 易写错 (结尾须 `)json"`);
  测试里改用普通转义字符串更清晰。
- 测试断言字符串字节数时, \u4E2D 解码为 3 字节 UTF-8, 需精确计数。

**下一步**
- IO 层续: config.json 解析 (超参→结构体) / tokenizer / 权重加载编排。

---

## 2026-09-03 — PLE 端到端 gather (PleEmbedding) 实现并通过真实文件验证 ★

**做了什么**
- 研读 sglang-ssd-stream 的 `reduce` (backend.py: TP=1 时 no-op) 与
  qwen4.py 的 lookup 准备/收尾, 确认输出布局 = [tokens, 2560]
  (16 head × 160 直接拼接, reader 已按 token-major 顺序 scatter),
  `weight_scale` 在 reduce 之后单独乘 (不在 gather 内)。
- 实现 `include/q4t/ple/ple_embedding.h` + `src/ple/ple_embedding.cpp`:
  PleEmbedding 编排类, 组合三个已验证构件:
  - ComputeRowIds (CPU, 纯函数): tokens + 每 token 2-token history →
    [n_tokens, 16] row_ids (token-major)。
  - GatherRows: PlePageReader 读 pinned staging → H2D → FP8→BF16 (stream)。
  - Gather: 组合 (1)+(2), 用 pinned host row_ids scratch 中转。
  - pinned staging (cudaHostAlloc) + GPU FP8 scratch (cudaMalloc),
    按 capacity_tokens 预分配。
- 测试 `tests/ple_e2e_gather_test.cpp`: 2 项 (与 CPU 参考对比全链路布局+
  数值; capacity 越界守护)。共 11 项测试全绿。
- **真实环境最终验证 (硬性约定 #7)**: 用真实 checkpoint 参数
  (multipliers/vocab/offsets/EOS) + 真实 51.2 GB sidecar 跑 Gather,
  6 token (含 1 个 EOS 边界) × 16 head × 160 字节, 与 pread 真实文件 +
  e4m3 解码逐字节一致。

**结论**
- **PLE 流式层 (核心特性) 全部完成**: ngram 哈希 / io_uring 读取器 /
  FP8→BF16 转换 / 端到端 gather, 每个构件 + 整条链路均在真实
  checkpoint 参数与真实 51.2 GB 文件上验证通过。

**踩坑**
- 手动 g++ 链接验证程序时, cuda_runtime.h 在
  `/usr/local/cuda-13.3/targets/sbsa-linux/include` (非顶层 include),
  需显式 -I 该路径。

**下一步**
- IO 层: safetensors 解析 (mmap) + JSON 配置 + tokenizer。

---

## 2026-09-03 — PLE FP8→BF16 CUDA 转换 kernel 实现并通过 GPU 验证

**做了什么**
- 研读 sglang-ssd-stream 的 Triton 转换 kernel (backend.py
  `_copy_ple_staged_rows_kernel`) 与 PLE 层 forward (qwen4.py),
  确认: 转换 kernel 只做 **FP8 e4m3 → BF16 纯类型转换**, `weight_scale`
  在 PLE 层 forward 的 16-head reduce 之后单独乘
  (`embeddings = reduce(rows) * weight_scale`)。因此 kernel 保持纯转换,
  与参考一致。
- 实现 `include/q4t/ple/fp8_convert.h` + `src/ple/fp8_convert.cu`:
  ConvertFp8ToBf16Async (每线程 1 字节, 用 `__nv_cvt_fp8_to_halfraw`
  官方 e4m3 解码, 在 side stream 上启动)。
- CMake: fp8_convert.cu 加入 q4t_ple, 链接 CUDA::cudart (头文件暴露
  CUDA 运行时)。
- 测试 `tests/ple_fp8_convert_test.cpp`: 2 项 (与 CPU e4m3fn 参考解码
  对比 13 个构造字节: 零/次正规/正规/负/最大有限/NaN; 空输入 no-op),
  真实 GPU 上逐字节一致。共 9 项测试全绿。

**踩坑**
- `__half` 无 `.x` 成员/默认构造, 需用 `__half(hraw)` 从 `__half_raw`
  构造。
- `std::ldexpf` 在 C++17 不可用, 用 `std::ldexp`(double) 转 float。
- 顺带修复 `ngram_hash_derive.cpp` 的 `-Woverflow` 警告: `1LL << 63`
  是未定义行为, 改用 `std::numeric_limits<int64_t>::max()`。

**下一步**
- PLE 端到端 gather: ngram 哈希 → reader → 转换 → weight_scale →
  key/value 投影, 对真实 51.2 GB 文件验证。

---

## 2026-09-03 — PLE io_uring SSD 读取器实现并通过真实文件验证

**做了什么**
- 研读 sglang-ssd-stream 的 `src/lib.rs` gather() 逻辑, 精确理解
  页切片/去重/批量波次读取/scatter 语义。
- 实现 PLE SSD 页读取器:
  - `include/q4t/ple/page_reader.h` + `src/ple/page_reader.cpp`:
    PlePageReader (Create/Gather/ReadStats)。
  - 行→4KiB 页对齐切片 (Piece{page_id, output_offset, page_offset, len})
    → 按 page_id 排序去重分组 (PageGroup) → io_uring 批量读
    (4096 页/批, 256 页/波) → scatter 到输出; 越界行输出置零。
  - 32MiB 注册页池: mmap(MAP_PRIVATE|MAP_ANONYMOUS) + MADV_DONTDUMP
    + io_uring_register_buffers (失败回退普通 Read)。
  - 文件 POSIX_FADV_RANDOM 提示。
- 测试 `tests/ple_page_reader_test.cpp`: 4 项 (基本行/页去重/越界置零/
  跨页行) 全过; 连同 ngram 哈希共 7 项测试全绿。
- 真实环境验证 (硬性约定 #7): 在真实 51.2 GB sidecar 上读 7 行
  (0/1 同页、25/26 同页、1000000、320001000、末行 320001535),
  与直接 pread 逐字节一致; 7 行 → 5 个唯一页 (去重正确)。

**踩坑 (重要)**
- `io_uring_submit_and_wait` 成功时返回**实际提交的 SQE 数**(≥0),
  不是 0; 初版用 `!= 0` 判断导致误报失败。正确判断: `< 0`。
- ring 版 buffer 注册函数是 `io_uring_register_buffers(ring, iov, nr)`,
  不是全局 `io_uring_register(fd, ...)` (后者是 fd 版)。
- 终端 cwd 反复被重置到无关目录, `cd` 被工具剥离; 一律用绝对路径
  (cmake -S /abs -B /abs, git -C /abs)。

**下一步**
- FP8→BF16 CUDA 转换 kernel (side stream)。
- PLE 端到端 gather: ngram 哈希 → reader → 转换 → key/value 投影,
  对真实文件验证。

---

## 2026-09-03 — Phase 1 启动: PLE ngram 哈希实现并通过测试

**做了什么**
- 检查 checkpoint 张量布局: 确认 PLE 派生 buffer 全部存在
  (`layer_multipliers` I64[3]、`ngram_heads_vocab_sizes` I64[16]、
  `ngram_heads_offsets` I64[16]、`weight_scale` BF16[1]), 运行时直接
  加载即可, 无需 sympy/splitmix64 重新推导。PLE 权重在 0-indexed
  `layers.1.ple.*` (ple_layer_ids=[2] 是 1-based)。
- 读取真实 checkpoint 数值: multipliers=[23703573157769,
  20109073645365, 8052911324071], 16 个素数词表 (20000003..20000171),
  offsets 累加; 验证 offsets[-1]+vocab[-1]=320001446 < sidecar 行数
  320001536 (差 90 = 向上取整到 128 倍数), 完全自洽。
- 用 SGLang 精确算法 (Python) 生成 5 组参考 row_ids 作为测试基准。
- 实现 PLE ngram 哈希:
  - `include/q4t/ple/ngram_hash.h` + `src/ple/ngram_hash.cpp`:
    ComputeNgramRowIds (含 EOS-ignoring shift 规则)。
  - `include/q4t/ple/ngram_hash_derive.h` + `.cpp`: splitmix64 派生
    multipliers (开发期交叉校验)。
  - 公共基础设施: `include/q4t/{status,log,test}.h`, 测试框架
    (Q4T_TEST/Q4T_CHECK, 无外部依赖)。
  - CMake 重构: 抽出 `q4t_ple` 静态库 + `q4t_tests` 可执行。
- 测试 `tests/ple_ngram_hash_test.cpp`: 3 项全过。

**踩坑 (重要)**
- 初版哈希把乘子 m[k] 乘到"较旧"的 token 上, 2-gram 全对但 3-gram 错。
  根因: SGLang 的 `_shift_right_ignore_eos` 不只是移位——**窗口内存在
  EOS 时, 跨越 EOS 边界的旧 token 会被替换成 EOS**。修正: 乘子 m[k]
  对应的 token (往回数第 k 个) 仅当它与当前 token 之间无 EOS 时取真实值,
  否则取 EOS。修正后与 SGLang 参考逐位一致。
- 验证方法: C++ 必须与 SGLang 参考算法在真实 checkpoint 参数下逐位
  一致 (5 组窗口含 EOS 边界), 这是 PLE 正确性的硬基准。

**下一步**
- io_uring SSD 读取器 (页去重 + 32 MiB 注册页池 + 4 KiB 页映射)。
- FP8→BF16 CUDA 转换 kernel。
- PLE 端到端 gather (对真实 51.2 GB 文件验证)。

---

## 2026-09-03 — PLE 机制破解: 研读 sglang-ssd-stream + SGLang qwen4_exp

**做了什么**
- 精读 `reference/sglang-ssd-stream` (qwen4.py / backend.py /
  lib.rs), 并拉取 SGLang `qwen4_exp.py` (commit 0a79825, 即
  sglang-ssd-stream 为 aarch64/Thor pin 的版本) 固化到
  `reference/sglang-qwen4-exp/` (含 PROVENANCE.md)。
- **完全破解 PLE 机制** (详见 MODEL.md):
  - PLE = **n-gram 哈希查找表**, 不是普通逐层嵌入。
  - 每 token 取 [t-2,t-1,t] 3-gram 上下文 (前 2 个来自
    per-request 2-token 历史缓存, EOS 边界不跨越)。
  - 16 个 head (2 阶 × 8): 每阶用 splitmix64 派生的奇数乘子
    做 XOR 混合, 对素数词表 (nth_prime_after(20M-1, h+1))
    取模 + offset → row_id。16 个词表之和 = 320,001,536 = 表行数。
  - 查 16 行 (160B FP8) → 2560 维 BF16 嵌入 →
    key_proj(→10240) / value_proj(→2560) → 与主干 4 分支
    hyper-connection hidden 做门控 (sigmoid 平滑) →
    depthwise conv1d (k=4, dilation=2, 零初始化, per-request
    state) → silu → 加到主干 (attn_hyper_connection.mix 之前)。
  - PLE 在 layer_id=2 (ple_layer_ids=[2]), 主模型 forward 循环
    中 layer i 执行前 prefetch layer i+1 的 PLE (独立 stream 重叠)。
- **确认 hc_*/indexer_* 语义**:
  - hc_count=4: 主干是 4 分支 hyper-connection (GatedResidual,
    每层 attn/mlp 各一个 mix/combine, 末尾 mixer 合成 2560)。
  - indexer_*: full_attention 层用 QSA 稀疏注意力 (indexer 算
    top-k KV 索引, budget=2048, compress_ratio=4), 注意力输出
    带 sigmoid gate。
- SSD Stream 读取机制确认: GPU ids → pinned host → 单线程
  io_uring 批量读 (4KiB 页去重, 32MiB 注册页池, FADV_RANDOM)
  → 2×16MiB pinned staging 轮转 → 独立 CUDA stream FP8→BF16
  → consumer wait event。

**为什么重要**
- PLE 是本项目核心特性, 此前 row_id 计算与融合方式完全未知,
  是最大技术风险。现已有权威参考 (SGLang 源码), 可精确复刻。
- 发现 full_attention 是 QSA 稀疏注意力 (非标准 dense GQA),
  实现复杂度高于预期, 已列入风险。

**下一步**
- 开始 Phase 1 实现 (顺序: IO → 量化 → PLE 流式 → 模型 → 引擎
  → 服务)。full_attention 前拉取 SGLang qsa 模块; linear_attn
  前研读 qwen35-thor deltanet。

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

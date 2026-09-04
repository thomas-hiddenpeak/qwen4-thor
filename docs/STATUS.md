# STATUS.md — 当前状态快照

> 本文始终反映"现在"。历史状态见 [LOG.md](LOG.md)。

## 当前阶段

Phase 1 — 核心推理引擎 + PLE SSD Stream + HTTP API
(详见 [PHASES.md](PHASES.md))

## 已完成

- [x] 2026-09-03 项目初始化: git 仓库、目录骨架、文档体系、构建配置
- [x] 2026-09-03 模型下载完成 (140 GB, 含 51.2 GB PLE sidecar,
  SHA-256 待验证)
- [x] 2026-09-03 参考项目调研 (qwen35-thor / sglang-ssd-stream /
  thor-probe / thor-bench)
- [x] 2026-09-03 构建骨架验证通过: `q4t version` / `q4t probe` 可运行
  (probe 正确识别 Thor SM 11.0, 20 SM, 122.9 GB)
- [x] 2026-09-03 参考项目克隆到 reference/: sglang-ssd-stream
  (176a522, v0.2.0), qwen35-thor (57e2977)
- [x] 2026-09-03 GitHub 仓库创建并推送 (thomas-hiddenpeak/qwen4-thor)
- [x] 2026-09-03 liburing 2.5 安装 (PLE io_uring 依赖)
- [x] 2026-09-03 PLE ngram 哈希 (row_id) 实现 + 测试:与 SGLang 参考
  逐位一致 (含 EOS-ignoring),multipliers 派生与 checkpoint 一致
- [x] 2026-09-03 PLE io_uring SSD 读取器实现 + 测试:4KiB 页切片/去重/
  注册页池/批量波次读取/scatter;真实 51.2 GB sidecar 上 7 行与 pread
  逐字节一致
- [x] 2026-09-03 PLE FP8→BF16 CUDA 转换 kernel 实现 + 测试: 纯 e4m3→
  BF16 类型转换 (weight_scale 在 PLE 层 forward 的 reduce 之后单独乘,
  与 SGLang 参考一致), 与 CPU e4m3 参考解码在真实 GPU 上逐字节一致
- [x] 2026-09-03 PLE 端到端 gather (PleEmbedding) 实现 + 真实文件验证:
  编排 ngram 哈希 → io_uring reader → FP8→BF16, 真实 checkpoint 参数 +
  真实 51.2 GB sidecar 上 6 token × 16 head × 160 字节与 pread+e4m3 逐字节一致
  **→ PLE 流式层 (核心特性) 全部完成**
- [x] 2026-09-03 IO 层核心: 最小 JSON 解析器 (递归下降, 含 \u 转义) +
  safetensors mmap 读取器 (头解析/张量元数据/按需读字节/H2D), 在真实
  模型 scale 文件上验证; 独立 `q4t_io` 静态库
- [x] 2026-09-04 tokenizer (GPT-2 Byte-Level BPE) 实现 + 差分验证:
  独立 `q4t_text` 库 (ICU 74 NFC 规范化 + `\p{L}` 预分词正则 + 优先队列
  BPE)。encode 做 added-token 整体子串匹配, decode 走 id→content, 均与
  transformers/tokenizers 一致。真实 tokenizer.json 上 57 个多样化输入
  与 python `tokenizers` 库逐位一致 (0 不匹配); 4 个单元测试通过
  **→ IO 层全部完成**
- [x] 2026-09-04 参考项目补全: Qwen3x-Orin (1688f50, tokenizer 参考) /
  thor-bench (3a33a90) / thor-probe (4816685) 克隆到 reference/,
  REFERENCE.md 更新
- [x] 2026-09-04 量化层前置验证: cuBLASLt 原生 NVFP4 (W4A4) 在 Thor
  SM110a 上跑通并数值正确。`CUDA_R_4F_E2M1` +
  `CUBLASLT_MATMUL_MATRIX_SCALE_VEC16_UE4M3`, 真实 expert 投影形状
  (gate/up N=640 K=2560, down N=2560 K=640) × M=1..256 共 16 例全部
  通过 (max_rel < 0.0001, 纯 FP4 量化噪声)。关键: scale 张量必须用
  **128×64 swizzle atom 布局** (非行主序), 物理大小 padding 到完整
  atom `ceil(rows/128)×ceil((K/16)/4)×512`。主数据保持行主序。
  **→ 原生 NVFP4 硬件路径确认可行 (用户要求: 利用硬件特性)**
- [x] 2026-09-04 量化层核心实现 (独立 `q4t_quant` 静态库):
  - `format.h`: e2m1 / UE4M3 编解码 (round-to-nearest-even, **无查表**,
    `__host__ __device__` 双端可用)。UE4M3 与 e4m3fn 正数位布局一致
    (max 0x7E=448, 0x7F=NaN), 与 checkpoint F8_E4M3 一致。
  - `swizzle.h`: NVFP4 scale 张量 swizzle 布局 (128×64 atom) + 物理大小
    padding 计算 + 行主序→swizzle 转换。
  - `dequant.cu`: NVFP4→BF16 dequant kernel (W4A16 路径, 含 global scale)。
  - `act_quant.cu`: BF16→NVFP4 运行时激活量化 kernel (W4A4 路径), 输出
    packed e2m1 + swizzled e4m3, e2m1 按 e4m3-rounded scale 舍入以与硬件
    逐位一致。
  - `fp4_gemm.h`: cuBLASLt 原生 W4A4 GEMM 封装 (CUDA_R_4F_E2M1 +
    VEC16_UE4M3, global scale 折进 alpha)。
  - 测试 `quant_fp4_test.cpp` 9 项: 格式 round-trip / swizzle 偏移+大小+
    round-trip / dequant kernel / 激活量化 kernel / **W4A4 GEMM 8 例**
    (真实 expert 形状 × M=1..256, max_rel < 2e-5)。35 项测试全绿。
  **→ 量化层核心完成 (NVFP4 W4A4 原生路径 + W4A16 dequant 路径)**
- [x] 2026-09-04 量化层收尾 (1/3): NVFP4 routed-expert MoE 权重加载
  编排 (`moe_weights.h/.cpp`, 独立于 `q4t_quant`)。
  - `MoEWeightLayout`: 每层 4 个大 device buffer — 合并 gate/up packed
    `[2*E*moe_is, hs/2]` 行主序 + per-expert swizzled SF 块、down packed
    `[E*hs, moe_is/2]` + per-expert swizzled SF 块、4 个 per-expert FP32
    标量 (weight_scale_2 / input_scale, device + host 副本)。
  - `LoadMoEWeights`: 单次遍历 512 expert, 直载 packed (跳过 ~73K 次
    cudaMalloc), gate/up 的 weight_scale 合并后 host 端 swizzle, down
    单独 swizzle, 标量 H2D + host 副本。
  - **关键约定**: gate_proj 与 up_proj 每 expert 共享相同
    weight_scale_2 / input_scale (checkpoint 已验证) → 合并成单个
    `[2*moe_is, hs]` GEMM 用单一 alpha; 每 expert 的 gate/up 切片步长
    是 `moe_is*hs` 字节 (2 个 proj 宽), 非 `moe_is*hs/2`。
  - 测试 `quant_moe_load_test.cpp` 4 项 (真实 checkpoint): packed 权重
    与 shard 逐字节一致 / SF 反 swizzle 还原到源字节 / gate-up 共享
    scale 全 512 expert 校验 / 加载 expert 的 W4A4 GEMM 与 CPU dequant
    参考一致 (max_rel 0.0)。39 项测试全绿。
  **→ MoE 权重 NVFP4 加载完成 (待补: grouped MoE GEMM 调度 + forward 接线)**

## 进行中

- Phase 1 实现:**PLE 流式层 (核心特性) 已全部完成** ✅ (ngram 哈希 /
  io_uring 读取器 / FP8→BF16 转换 / 端到端 gather, 均通过真实 checkpoint
  参数 + 真实 51.2 GB sidecar 验证)。
- Phase 1 实现:IO 层。
  - ✅ 最小 JSON 解析器 (递归下降, 含 \u 转义/数字/对象/数组/错误定位)。
  - ✅ safetensors mmap 读取器 (头解析/张量元数据/按需读字节/H2D),
    在真实模型 scale 文件上验证。
  - ✅ config.json 解析 (ModelConfig 结构体 + 不变量校验), 在真实
    config.json 上验证全部关键超参 (48 层 / hidden 2560 / MoE 512×10 /
    PLE / QSA indexer / MRoPE / MTP / NVFP4)。
  - ✅ 权重加载编排 (WeightIndex + WeightLoader): 解析 index.json
    (296347 张量 → 197 shard), 按需 mmap shard + LRU 缓存, 读取与直接
    打开 shard 逐字节一致。
  - ✅ tokenizer (GPT-2 Byte-Level BPE, ICU 74 NFC + \p{L} 正则):
    独立 `q4t_text` 库。encode 做 added-token 整体子串匹配 (与
    transformers/tokenizers 一致), decode 走 id→content。在真实
    tokenizer.json 上 57 个多样化输入 (空串/CJK/emoji/NFC 组合/特殊
    标记/长文本) 与 python `tokenizers` 库逐位一致, 0 不匹配。
  **→ IO 层 (JSON / safetensors / config / 权重 / tokenizer) 全部完成** ✅
- Phase 1 实现:量化层核心。
  - ✅ `format.h` e2m1 / UE4M3 编解码 (round-to-nearest-even, 无查表,
    `__host__ __device__`)。
  - ✅ `swizzle.h` NVFP4 scale swizzle 布局 + padding + 转换。
  - ✅ `dequant.cu` NVFP4→BF16 (W4A16)。
  - ✅ `act_quant.cu` BF16→NVFP4 运行时激活量化 (W4A4)。
  - ✅ `fp4_gemm.h` cuBLASLt 原生 W4A4 GEMM 封装。
  - ✅ `moe_weights.h/.cpp` routed-expert NVFP4 权重加载编排 (4 大 buffer
    直载 + gate/up 合并 + per-expert swizzled SF + 标量), 4 项真实
    checkpoint 测试 (packed/SF/共享 scale/GEMM 参考) 全过。
  - ✅ 13 项量化测试 (9 核心 + 4 MoE 加载), 39 项全绿。
  **→ 量化层核心 + 权重加载完成** ✅ (待补: grouped MoE GEMM 调度 +
  input_scale 在 forward 接线)

## 阻塞 / 风险

- **PLE sidecar SHA-256 未验证** (ssd-stream.json 记录了期望值
  `b070f964...`, 51.2 GB 校验耗时较长, 安排在首次加载前完成)。
- **QSA 稀疏注意力细节**: indexer 的 top-k 选择算法在 SGLang 的
  `sglang/srt/layers/attention/qsa/` 模块 (尚未拉取), 实现
  full_attention 层前需研读。
- **DeltaNet SSM 细节**: linear_attention 继承 Qwen3.5 的
  GatedDeltaNet, 实现前需参考 qwen35-thor 的 deltanet 实现。

## 已解决 (2026-09-04)

- ✅ **constexpr 查表数组在 device 端无存储**: `format.h` 初版用
  `inline constexpr float kE2m1Table[16]` / `kE4m3Pow2[15]` 做运行时索引
  解码。CMake 构建 (带 `--expt-relaxed-constexpr`) **编译通过**, 但
  namespace 作用域的 constexpr 数组在 device 代码里**没有存储**, 运行时
  索引读到垃圾 → dequant/act_quant kernel 输出全 0 或错值, 而 W4A4 GEMM
  测试仍 PASS (cuBLASLt 用硬件自己的 e4m3 解码, 不调用我的函数), 掩盖了
  bug。修复: 解码改为**无查表** (e2m1 按位分解 + `ldexpf`, e4m3 按位 +
  `ldexpf`), 全部 `__host__ __device__`。教训: ① 不要在 device kernel 里
  用运行时索引的 constexpr 数组; ② 单元测试必须直接验证 kernel 输出,
  不能只靠端到端 GEMM (会掩盖底层格式错误)。
- ✅ **cuBLASLt 原生 NVFP4 scale 布局破解**: `VEC16_UE4M3` 的 scale
  张量**不是行主序**, 而是硬件 tcgen05.mma 要求的 **128 行 × 64 元素
  swizzle atom** (CUTLASS `SfKMajorAtom`)。逻辑坐标 (r 行, g 组, g=K/16)
  → offset: `i=r%32, j=(r%128)/32, ga=g%4, within=i*16+j*4+ga,
  offset=within+(g/4)*512+(r/128)*((K/16)/4)*512`。物理大小必须 padding
  到完整 atom: `ceil(rows/128)*ceil((K/16)/4)*512` (即使 M=8 也按 128
  行分配, 否则堆越界)。主数据 (FP4 packed) 保持行主序 [N, K/2]。
  用 CuTe `tile_to_shape(SfAtom, (M,K), Step<_2,_1>)` 探针验证公式,
  16/16 真实形状用例通过 (max_rel < 0.0001)。

- ✅ **MoE 合并 gate/up 切片步长 bug**: `gu_packed_expert(e)` 初版用
  `e * moe_is * hs/2` (单 proj 步长), 但每 expert 的合并切片含 gate+up
  共 `2*moe_is` 行, 步长应为 `e * moe_is * hs`。错误导致 expert e≥1 的
  gate 覆盖 expert e-1 的 up。W4A4 GEMM 测试**没抓到** — 它只用 expert 0
  (偏移 0, 不受步长影响) 且是 device buffer 自洽对比 (CPU 参考也 dequant
  同一 buffer)。修复后靠 `moe_load_packed_matches_shard` 对 expert
  {0,100,511} 的 gate/up/down 与 shard 逐字节比对才暴露。教训: ① 步长/
  偏移类 bug 必须用**多个非零索引** + **与独立数据源 (shard) 比对** 才能
  抓到, 单点 + 自洽对比会掩盖。

## 已解决 (2026-09-03)

- ✅ **PLE 查找机制完全理解**: PLE = n-gram 哈希查找表。
  每 token 取 [t-2,t-1,t] 3-gram 上下文, 16 个 head 各算一个
  哈希 row_id (splitmix 派生乘子 + 素数词表取模), 查 16 行
  (160B FP8) 拼成 2560 维嵌入, 经 key/value 投影 + 门控 +
  depthwise conv 后加到主干。详见 MODEL.md。
- ✅ **hc_*/indexer_*/ngram_* 字段语义确认**:
  hc_count=4 (hyper-connection 4 分支主干), indexer_* = QSA
  稀疏注意力索引器配置, ngram_* = PLE 查找参数。
- ✅ **PLE io_uring 读取器实现并验证**: 镜像 sglang-ssd-stream 的
  gather 语义 (行→4KiB 页对齐切片 Piece → 按 page_id 排序去重分组
  PageGroup → 4096 页/批、256 页/波 io_uring 读 → scatter 到输出;
  越界行输出置零)。32MiB 注册页池 (mmap + io_uring_register_buffers,
  失败回退普通读)。真实 51.2 GB sidecar 上 7 行 (0/1 同页、25/26 同页、
  1000000、320001000、末行 320001535) 与 pread 逐字节一致。
  坑: `io_uring_submit_and_wait` 成功时返回**提交的 SQE 数**(≥0),
  非 0; 错误判断须用 `< 0`。

## 下一步

1. 继续 Phase 1 实现。**PLE 流式层** ✅, **IO 层** ✅, **量化层核心 +
   MoE 权重加载** ✅ (NVFP4 W4A4 原生路径 + W4A16 dequant, 39 项测试
   全绿)。
   建议顺序:
   - **量化层收尾 (续)**: grouped MoE GEMM 调度 (512 expert top-10 +
     shared expert, 复用 `MoEWeightLayout` + `Fp4Gemm`) / input_scale 在
     forward 的接线 (激活量化用 per-expert input_scale)。
   - **模型层**: 48 层 forward (DeltaNet / QSA full-attn / MoE /
     hyper-connection / PLE 融合)。
   - **引擎层**: 请求生命周期 + MTP + 采样。
   - **服务层**: OpenAI 兼容 HTTP API。
2. 实现 full_attention 前, 拉取 SGLang qsa 模块研读 indexer。
3. 实现 linear_attention 前, 研读 qwen35-thor 的 deltanet 实现。

## 环境

| 项 | 值 |
|---|---|
| 硬件 | Jetson AGX Thor, SM110a, 20 SM, 122 GB LPDDR5X |
| 驱动 / CUDA | 595.78 / 13.3 (nvcc 13.3.33) |
| CMake / GCC | 3.28.3 / 13.3.0 (aarch64) |
| ICU | 74.2 (tokenizer NFC + 正则; 仅 C API `uregex_*` 可用,
  精简安装缺 C++ 类头 `regexpattern.h`) |
| liburing | 2.5 (PLE io_uring) |
| 模型路径 | `~/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream` (只读) |
| 磁盘 | NVMe, 约 360 GB 可用 |

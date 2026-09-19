# AGENTS.md — Qwen4-Thor 开发入口

任何 agent 进入本项目, 先读本文, 再按需要读 docs/ 下对应文档。
**代码是最高事实来源**; 文档与代码不一致时, 以代码为准并修正文档。

## 项目一句话

在 Jetson AGX Thor (SM110a) 上用 C++23 (host + device, CUDA 13.3) 实现
Qwen3.8-Flash-Next (qwen4_exp) 的原生推理引擎,
核心特性是 PLE SSD Stream (51.2 GB FP8 查找表从 NVMe 异步流式读取)。

## 文档导航

| 想了解什么 | 读哪个文档 |
|---|---|
| 当前进展到哪了、下一步做什么 | [docs/STATUS.md](docs/STATUS.md) |
| 整体架构设计 | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| 分阶段计划与范围 | [docs/PHASES.md](docs/PHASES.md) |
| 开发日志 (按日期分文件, 时间倒序) | [docs/log/](docs/log/README.md) |
| 已完成 / 已解决归档 | [docs/DONE.md](docs/DONE.md) |
| 目标模型架构细节 (qwen4_exp) | [docs/MODEL.md](docs/MODEL.md) |
| 参考项目说明 | [docs/REFERENCE.md](docs/REFERENCE.md) |
| 文档总索引 | [docs/README.md](docs/README.md) |

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
6. **每次有意义的改动后**, 更新 `docs/STATUS.md` 并追加一条开发日志到
   当天 `docs/log/<日期>.md` 顶部 (不存在则新建并在 `docs/log/README.md`
   索引登记); 每条: 日期、做了什么、为什么、下一步。日志只追加不改历史。
7. **验证以真实环境为准**: 开发过程直接在本机 Thor 上构建、运行、
   测试, 不假设 CI 环境。

## 快速上手

```bash
# 构建 (要求: CMake >= 4.0 [pip 装 4.4.3] + g++-14 [apt]; C++23 host+device)
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_CXX_COMPILER=g++-14 -DCMAKE_CUDA_HOST_COMPILER=g++-14 \
  -DQ4T_CUDA_ARCHITECTURES=110a
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
  官方实现, 含首个 full_attention) / **decode 路径正确性 (已闭合)**
  (conv1d 窗口 + PLE short-conv 持久状态两个 decode bug 已修复;
  E1–E10 实验链定性 batch vs incremental 残差 = MoE 路由边界敏感性,
  非状态 bug: 增量自洽 0.000173 / 对参考等距 0.1306≈0.1309 / 全位置
  MoE 翻转对照; C++ vs 参考 16 步 12/16 argmax, 不匹配均为 near-tie
  被 NVFP4 噪声翻转) / **prefill/decode 性能优化** (decode 6.2→14.6
  tok/s, prefill 18.4→35.5, 接近带宽下限; 手写 kernel 全面覆盖 BF16
  路径: GEMV warp-per-output+f32x2 / GroupedRmsNorm warp-per-branch /
  SparseAttention 去冗余 / RouterTopk 并行化; FP4 W4A4 GEMM 保留 nvjet
  tensor core, 已证手写 SIMT 无法超越) / **MTP 推测解码** (draft k
  步 + 主模型验证 + 接受/回退 + recurrent 状态快照/恢复) / **多模态
  图像输入 (已闭合)** (27 层 ViT CUDA 实现, CUDA vs numpy l2_rel=
  0.0317; image token 248056 位置 embedding 替换为视觉特征, 镜像 vllm
  `_merge_multimodal_embeddings`; C++ 图像 processor: stb 解码 +
  Pillow 12.3.0 定点 BICUBIC + block-major patchify, 对真实 transformers
  processor 逐位一致; serve 层 OpenAI image_url/base64 接入 + 端到端
  图像→特征→注入→生成) / **多模态视频输入 (已闭合, Phase 2)** (27 层 ViT
  逐时间组注意力 + t-major pos; C++ ProcessVideo 视频 processor; serve 层
  视频 part 接入 + 混合图像/视频 batch + 按位置展开; 占位符实为
  |image_pad| 248056 / |video_pad| 248057, 修复了旧代码塞 <image> 被 BPE 的
  既有 bug) / **多模态 3D MRoPE (已闭合)** (视觉/视频 token 的 RoPE 从纯
  文本逻辑位置升级为 transformers 5.16.1 3D MRoPE (t,h,w 三行 +
  mrope_position_delta); 布局统一 [3,max_len] 绝对位置, prefill/decode/
  压缩 key 共用一张表, 消除 decode 越界隐患; MTP 恒等表 + 修正漏传
  rope_pos; 差分测试 vs Python 参考 63 坐标逐位一致, 纯文本无回归) /
  **验证标准体系 (已闭合, Phase 2)** (tools/verify/ 三件套: 主驱动 +
  transformers 5.16.1 参考 (逐层 lazy dequant, 全 48 层) + 三判据对比
  (置信位置 argmax / near-tie 翻转 / l2_rel 噪声带, 带退出码); 48 层
  全量基线 OVERALL PASS: 置信位置 44/44 全对 + 8 翻转全 near-tie +
  l2_rel 0.140 在噪声带; GPU 参考已评估, 留待连续批处理启动时落地) /
  **PLE 工作内存 + SHA-256 (已闭合)** (工作内存
  实测 75.17 MiB < 100 MiB 预算, 无 OOM 无 swap; 51.2 GB sidecar SHA-256
  与 MODEL.md 期望值逐位一致) / **greedy 生成输出与参考一致 (已闭合,
  L2 噪声保真度)** (C++ NVFP4 W4A4 vs transformers 5.16.1 参考, 同一
  256-token prompt 16 层 prefill logits 对比; 置信位置 argmax 8/8 全对 +
  108 个翻转全 near-tie + l2_rel 0.207 在 W4A4 噪声带; 无系统性错误,
  差异纯为量化噪声) / **B1 多序列隔离 (已闭合, Phase 2)** (per-seq
  recurrent-state 池化 max_seq + seq_id 穿透 + 多序列隔离测试 + serve
  多请求 E2E; 修 uint16_t 字节步长越界 + float atomicAdd 非确定两 bug)
  / **B2 连续批处理 (已闭合, Phase 2)** (B2a 引擎: token 级打包, B 序列
  各 1 decode token 一次 T=B forward, 权重只读一次; GEMM 无状态自动
  打包, 有状态 kernel 用 d_seq_id[t] 选 per-token 切片; B2b serve 调度器:
  独立调度线程合并并发请求 decode step; E2E 3 并发请求语义正确无跨序列
  污染, 吞吐 15.13→18.03 tok/s 聚合) / **MTP 批处理 Stage 1 (已闭合,
  Phase 2)** (draft 模型 KV/indexer/rope 按 max_seq 池化 + MtpForward
  d_seq_id 多序列路径, 单序列 bit 不变; 隔离测试 4 序列×2 token 打包
  l2_rel≈0.002 无跨序列污染) / **MTP 批处理 Stage 2a (已闭合, Phase 2)**
  (ModelVerifyMulti: B 序列 × (k+1) token 打包一次主模型 forward,
  sequence-major; linear/PLE per-seq 因果链 kernel + full attn per-seq
  paged KV; checkpoint 池化布局 [num_layers, max_seq, cap, elems] + 新增
  PLE conv checkpoint (修复单序列部分接受 PLE conv 残留 bug); 修 2 个
  kernel bug: PLE 因果 conv 的 T 参数边界/局部位置混用 (seq≥1 跨序列读)
  + PLE conv 状态更新缺 T<state_len 滑窗; 测试 B=2 不同 prompt × T=3
  verify logits vs 单序列 prefill 参考 6/6 bit-exact + 回滚 0.00896) /
  **MTP 批处理 Stage 2b (已闭合, Phase 2)** (MtpSpeculativeStepMulti:
  B 序列一步投机解码, 三段批量化 — 批量化 draft 循环 (per-seq 滚动 trunk,
  k 次 forward 替代 B×k) + ModelVerifyMulti 验证 (per-seq checkpoint 各自
  回滚) + 批量化 extend (接受前缀连续打包 T=Σ(a_b+1) 一次 MtpForward +
  GatherTrunkRowsKernel 收集 verify trunk 行); 新增持久多序列 scratch
  d_ms_* + 修 MtpModel::Free 既有 d_spec_multi 泄漏; 修 2 个 bug:
  RunLayers logits==nullptr 仍调 HeadForward 触发 CUBLAS INVALID_VALUE
  (trunk-only 路径) + MtpSpeculativeStepMulti 初版 PLE history=nullptr
  致 n-gram 上下文全 EOS 填充 (logits 全错); 测试 mtp_spec_multi_step
  B=2 不同 prompt 长度 4/5 × k=3 vs prefill 语义贪心 ground truth 两序列
  accepted/next_b 全匹配无跨序列污染) / **MTP 批处理 Stage 2c (已闭合,
  Phase 2)** (调度器 MTP 分支: SchedulerLoop 把 pending 请求 split 成
  mtp/plain, MTP 批量跑一次 MtpSpeculativeStepMulti (B 序列共享投机步) +
  plain 跑 ModelDecodeBatchMulti; HandleChat MTP 投机循环改注册调度器 +
  阻塞 cv, 请求线程推进 seq; ActiveRequest 加 ModelSequence* seq 指针;
  MtpSpeculativeStepMulti 签名 seqs 改 const ModelSequence* const* + 全用
  真实 seqs[b].seq_id (初版用 batch 索引 b 当 seq_id, serve free pool 下
  seq_id≠b 会污染); verify/extend buffer 持久化 + MtpDraftExtend 加
  seq_id + serve MTP 路径 per-seq 化; 5 增量提交 ef1510b/9d27be0/
  ad655cf/7a05e41/4b; serve 3 并发 MTP × 3 轮语义正确且确定无跨序列污染;
  吞吐聚合 ~22 vs 单 ~21.8 tok/s 提升有限, 根因 MTP 步长错位 (各请求每步
  接受数不同 → 投机步天然不同步, B 分布 81×B=1+38×B=2), 属 MTP 投机解码
  固有特性非 bug) / **MTP 调度 lockstep (已闭合, 计划 A, 2026-09-14)**
  (修 4b 吞吐收益小的根因: 调度器 MTP 等待谓词从"任意 pending 即跑"改
  "所有活跃 MTP 都 pending 才跑" (plain 仍机会式), 批量化步 B 恒等于活跃
  MTP 请求数 (uniform 步宽, 对齐 vllm/sglang); 实测 3 并发 B 分布
  81×B=1+38×B=2+0×B=3 → 5×B=1+16×B=2+37×B=3, 聚合吞吐 ~21.8 → 29.9
  tok/s (1.37×), wall 25-30s → 20.1s, 单请求无回归, 0 error;
  Q4T_SCHED_DEBUG=1 打印每步 B; 参考调研 docs/REFERENCE_MTP.md, 参考项目
  已更新 vllm 09-14 + sglang-ssd-stream v0.3.0) / **计划 D + 锁步死锁修复
  (已闭合, 2026-09-14)** (计划 D: MtpSpeculativeStepMulti draft 循环
  GPU-resident 化 — d_ms_drafts [max_seq,k_max] + Gather/Scatter/GatherMatrix
  3 kernel, 循环内零 host 同步, 对齐 vllm; 吞吐收益≈0 (draft 仅占投机步
  ~8%, verify 占大头, 计划 D 优化错目标但代码更干净保留)。锁步死锁修复
  (计划 A 遗留 bug): MTP 请求从 active_ 移除后补 sched_cv_.notify_one() —
  计划 D 首版 E2E 3 并发 1 请求挂死 300s, B 序列 B=1,B=3×37,B=2×12,卡死
  定位为丢失唤醒竞态 (c 的 notify 在调度器未入 wait 时丢失 → X 移除未
  notify → 谓词转 true 无人唤醒); 修复后 3 轮×3 并发×200 tok 全完成
  31.0/29.9/31.1 tok/s, B 分布 34×B=1+46×B=2+190×B=3, 0 error 输出确定)
  / **性能 profile 收尾 (已闭合, 2026-09-14)** (多序列 MtpSpeculativeStepMulti
  加 Q4T_MTP_TIMING 分段计时; nsys per-kernel 两 regime 实测: 短序列
  verify 占 84% GEMM 权重带宽主导 indexer 仅 0.2%, 长序列 3707 tok
  SparseAttentionKernel 占 71.4% 但 prefill 主导, indexer 仅 2.5%;
  **计划 B 否决** — indexer 两 regime 都非瓶颈, 此前"长序列 indexer
  61-73%"判断引用了 bitonic sort 并行化前的旧注释, 实测已降 0.2%。
  真瓶颈: decode 吞吐 = GEMM 权重带宽 (方向 FP8 计划 C) / prefill TTFT
  = SparseAttentionKernel (方向 profile 后优化)。68 项测试全绿零警告) /
  **262K 内存预算评估 + PD 决定 (2026-09-14)** (262144 上下文: 36 层
  linear SSM O(1) 不随序列增长, 仅 12 层 full attn KV+indexer 随 max_len
  线性; ground truth 82.4 GB @ 8192×seq4 → 262144 时 max_seq=1 可行
  ~101 GB / max_seq=4 OOM ~158 GB; 需分块 prefill (一次性 [T,vocab]
  logits 130 GB 不可行); QSA idx_budget=2048 在 262K 只 attend ~3% 历史
  召回受限。PD 分离: 当前 PD-ready 架构已满足本机调度, 多设备/双机降级
  后续计划。详见 PHASES.md "长上下文 262K 内存预算") /
  **QSA 长上下文召回修复: 流式 top-k (已闭合, 2026-09-18)** (移除 8192
  硬上限: 旧 kMaxBlocks=2048 把候选块锁死前 2048 压缩块, 8192 之后近期
  上下文从不被打分, 长上下文召回断裂 — 我们的不完整实现 bug 非 SGLang
  设计, 参考 tokenspeed/vllm/sglang-ssd-stream 都对全部 num_blocks 打分
  无上限。prefill 不能 materialize [T,all_blocks] (T=8192 时 3.3TB), 镜像
  tokenspeed split+merge-tree 做流式 top-k: CHUNK=2048 循环覆盖所有块,
  每块 tensor-core GEMM 打分 + merge 进 running top-512, 末尾 expand +
  当前 group 尾部; ≤8192 走原 single-shot 路径不变零回归, >8192 走流式;
  新增 BitonicSortAsc/InitRunTopk/MergeChunkTopk/ExpandRunTopk 4 kernel,
  IndexerLogits/Reduce 加 block_off 打分全局块去 cap。验证: 流式合并=精确
  全局 top-512 (host 暴力自检, 多 chunk id_diff 全边界 near-tie
  far_from_boundary=0 max_logit_diff≤0.00011) + needle @9000 (group 2250>
  2048 旧代码 100% 不可见) 召回 + 76 测试全绿零警告。已知边界非 bug: QSA
  top-512 选择性稀疏, 块数远大于 512 且 needle 不够突出时可能跌出, 参考无
  大 recent window 与 vllm/sglang 一致)。
- **Phase 1 完成标准全部闭合 (2026-09-07)**。Phase 2 进行中: B1 多序列
  + B2 连续批处理 + MTP 批处理 Stage 1 + Stage 2a (多序列验证前向) +
  Stage 2b (多序列投机步) + Stage 2c (调度器 MTP 分支, 并发 MTP 请求共享
  投机步) + 计划 A (调度 lockstep, 并发 MTP 聚合吞吐 1.37×) + 计划 D
  (draft 循环去 host 同步, GPU-resident, 吞吐收益≈0 但代码更干净) 已闭合。
  剩余: 性能优化 (decode GEMM 权重带宽 → FP8 计划 C / prefill
  SparseAttentionKernel → 待 profile) + Phase 2 功能 (长上下文 262K 验证
  进行中 / 完整多设备 PD 部署降级后续计划), 见 docs/PHASES.md +
  docs/REFERENCE_MTP.md。计划 B (MTP 复用 QSA top-k 索引) 已否决
  (indexer 非瓶颈, 见 LOG.md 2026-09-14 profile 条目)。视频输入 + 验证
  标准体系 + 连续批处理均已闭合 (PHASES.md 已更新)。

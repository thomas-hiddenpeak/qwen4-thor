# MoE 分层驻留 — 262144 容量完整内存账（2026-10-01）

目标要求：先建立 262144 容量下**可复核**的完整内存账，再确定各层席位与
策略。本账分两部分：预算侧（代码可复核，逐项给出字节数与来源）与实测侧
（monitor_memory.py 权威口径）。统一内存（Thor LPDDR5X 统一内存，
MemTotal 131.92 GB），避免重复计数；整机 RAM 与 swap 分列记录。

## 0. 测量口径

- 权威指标：`service_total_physical_peak_bytes` = 同一时刻
  `rss(服务进程树) + gpu(nvidia-smi 逐进程)` 的时间峰值
  （tools/evalscope/monitor_memory.py，1 Hz 采样）。
- GPU 侧：nvidia-smi `--query-compute-apps=pid,used_memory`（Thor 统一
  内存下 CUDA 分配的唯一可靠来源）。
- 模型相关页缓存：NVMe 权重读取产生的 page cache（/proc/meminfo Cached
  增量），与 rss 的 file 页可能重叠，**不重复计入**权威指标；单列报告。
- swap：`SwapTotal − SwapFree`，单列报告，不用于掩盖物理超支。

## 1. 预算侧（代码可复核）

来源：src/runtime/memory_budget.cpp（常量与公式）、
src/server/chat_server.cpp:138-156（驻留模式权重扣减与 L2 计费）。
预算 = 90% × MemTotal = 118.73 GB。

### 1.1 公共项（两配置相同）

| 项 | 字节 | 来源 |
|---|---:|---|
| 主 workspace d_ws (@8192) | 2,683,503,000 | memory_budget.cpp:22 kMainWorkspaceBytes |
| forward 缓冲 (@8192) | ≈398,700,000 | ForwardBufferBytes()，2026-09-19 实测 |
| MTP workspace（预算恒计） | 5,000,000,000 | kMtpWorkspaceBytes；MTP 关时预算仍保留（保守项，待条件化核实） |
| PLE working（staging+gpu_fp8+ring） | 75,170,000 | kPleWorkingBytes |
| CUDA context + 系统余量 | 2,000,000,000 | kContextMarginBytes |
| state pool (262144, seq=1) | 8,860,000,000 | StatePoolBytes()，服务日志实测 8.86 GB |
| per-request trunk+draft_logits | 9,440,000,000 | PerRequestBytes()，服务日志实测 9.44 GB |

### 1.2 权重

| 项 | 基线 C=0 | 候选 C=256 | 来源 |
|---|---:|---:|---|
| 模型索引 total_size | 83,995,036,096 | 83,995,036,096 | model.safetensors.index.json metadata |
| 路由专家扣减 (512−C)×48×2,764,816 | 0 | 33,977,725,696 | chat_server.cpp:144-152（每专家文件尺寸 2,764,816 B = gu 1,638,400 + dn 819,200 + gu_sf 204,800 + dn_sf 102,400 + 4×f32 16） |
| 预算计权 | 83,995,036,096 (84.00 GB) | 50,017,310,400 (50.02 GB) | 服务启动日志 [q4t][budget] weights= |

非路由权重 = 83,995,036,096 − 48×512×2,764,816 = 16,046,918,080 B (16.05 GB)：
embed/lm_head/attn/linear/HC/PLE/共享专家。

### 1.3 驻留模式附加项

| 项 | 第一轮二进制 (509665d) | 第二轮二进制 (35b9f7d+) | 来源 |
|---|---:|---:|---|
| pinned staging | 48×8×3,276,816 = 1,258,297,344 B（每层每加载 worker 1 块，默认 8 worker） | —（并入 L2 池） | moe_residency.cpp Init（509665d 版） |
| L2 pinned 专家缓存 | — | 48×128×3,276,816 = 20,132,757,312 B (20.13 GB) | moe_residency.cpp:184 cudaHostAlloc(l2_slots_×staging_bytes_)；Q4T_MOE_L2_SLOTS 默认 128 |
| 预算计费 extra_fixed | 48×3,276,816 = 157,287,168 B | 48×128×3,276,816 = 20,132,757,312 B | chat_server.cpp:150-153 |

注意：第一轮预算对 staging 少计（计 1 块/层，实配 8 块/层，差 ≈1.10 GB）；
第二轮 L2 池预算与实配一致。staging 单块 3,276,816 B =
MoEResidencyStagingBytes(2560, 640)（文件序布局 [dn|ga|up] 权重 +
原始 SF + gu 合并暂存 + swizzle SF 块 + 4 标量）。

### 1.4 预算合计（262144, max_seq=1, MTP 关）

| 配置 | weights | fixed（含公共项） | per_request | state_pool | 预算占用 |
|---|---:|---:|---:|---:|---:|
| 基线 C=0 | 84.00 GB | 94.15 GB | 9.44 GB | 8.86 GB | 112.45 GB / 118.73 GB |
| 候选 C=256（第一轮） | 50.02 GB | 60.34 GB | 9.44 GB | 8.86 GB | 78.66 GB / 118.73 GB |
| 候选 C=256+L2-128（第二轮） | 50.02 GB | 80.47 GB | 9.44 GB | 8.86 GB | 98.79 GB / 118.73 GB |
| 候选 nu-15552+L2-8（第三轮，已授权） | 59.05 GB | ≈70.62 GB（估） | 9.44 GB | 8.86 GB | ≈88.92 GB / 118.73 GB |

（fixed = weights + 公共项 + extra_fixed；服务启动日志 [q4t][budget] 为
逐项实测输出，基线/候选日志已存于 e2e-*/server.log。）

nu-15552 预算推导（代码可复核）：常驻专家权重 = 15552 × 2,764,816 B =
42,998,418,432 B（42.998 GB；按层 C_l 256..446 之和 = 15552，DP 最优，
tools/trace/make_hot_list_nu.py）；非路由权重 16,046,918,080 B（16.05
GB）→ weights = 59,045,336,512 B（59.05 GB）。L2-8 extra_fixed =
48×8×3,276,816 = 1,258,297,344 B（1.26 GB）。公共项按第二轮服务日志
隐含值 ≈10.32 GB（第二轮二进制公共项不变）。fixed ≈ 70.62 GB、预算
合计 ≈ 88.92 GB 为估算，**第三轮二进制启动后以 [q4t][budget] 日志逐项
核对回填**（chat_server.cpp 预算估算已改按层实际驻留数，62edd6a）。
实测峰值估算：第一轮 C=256 实测 58.49 GB + Δ专家权重 9.02 GB +
L2-8 1.26 GB ≈ 68.8 GB（用户已接受该超支，2026-10-01 07:25 决定）。

## 2. 实测侧（权威）

monitor_memory.py，矩阵 e2e-baseline-s0-current / e2e-cand-c256
（23:46 二进制，sha256 4c5cddea，commit 509665d）。

| 指标 | 基线 C=0 | 候选 C=256（第一轮） |
|---|---:|---:|
| service_total_physical_peak_bytes（rss+gpu 峰值） | 91,331,678,208 (91.33 GB) | 58,490,000,000 量级（58.49 GB，矩阵进行中，峰值已稳定于启动+1024 档） |
| gpu_peak_bytes | 90,399,834,112 (90.40 GB) | 56.24 GB |
| rss 峰值（服务进程树） | 1.78 GB (hwm) | 2.20 GB |
| 模型相关页缓存峰值（Cached 增量，单列） | ≈32.2 GB | ≈16.4 GB |
| swap 使用峰值 | 1.46 GB | 1.38 GB |
| 整机 MemTotal / MemFree 低值 | 131.92 GB / ≥5.5 GB | 同左 |

历史同口径参考（exp12，16:42，同二进制族）：候选 57.71 GB、基线 91.33 GB。
第二轮（L2-128）预计峰值 ≈ 58.5 + 20.13 ≈ 78.6 GB（L2 池启动即全额
pinned 分配），以实测为准。

## 3. 核对与差异

1. 预算占用（1.4 节）> 实测峰值：预算含 MTP workspace 5 GB（MTP 关时
   是否实际分配待核实）、context 余量 2 GB 等保守项；预算是容量判定
   （max_len 不被 cap）的依据，不是实测峰值。
2. 基线实测 GPU 峰值 90.40 GB vs 预算权重+workspace+state 合计 ≈96 GB：
   差 ≈5.6 GB 未逐项归因（可能为延迟分配/nvidia-smi 口径），列为后续
   审计项，不影响本账的实测权威性。
3. 候选实测 GPU 峰值 56.24 GB ≈ 非路由权重 16.05 + C=256 槽 33.98 +
   workspace/forward/state/PLE ≈6.2 + 余量，量级一致。
4. 页缓存与 rss file 页重叠部分不重复计入权威指标；页缓存峰值单列
   （基线 32.2 GB / 候选 16.4 GB），候选页缓存更小因常驻权重更少、
   NVMe 读取更集中于补载。

## 4. 决策记录

- 54 GB 门槛对 C=256 超支（实测 57.71–58.49 GB vs 54 GB）：用户
  2026-09-30/10-01 明确接受，验收报告如实报告实测峰值并标注本决定。
- 第二轮 L2-128 使峰值预计升至 ≈78.6 GB：超出用户已接受的 57.71 GB
  量级，属新增超支；若矩阵确认性能收益必要，需用户再次确认，或降
  Q4T_MOE_L2_SLOTS（64/层 → +10.07 GB；32/层 → +5.03 GB）。
- 第三轮 nu-15552+L2-8（≈68.8 GB，超已接受 C=256 的 58.49 GB 约
  10.3 GB）：**用户 2026-10-01 07:25 明确接受超支**（"即使内存预算
  超支，我支持你把C=256也做了，每层专家按照命中次数的top-n来"），
  第三轮矩阵（r3-cand-c256 + r3-cand-nu15552，均 L2-8）已获授权，
  auto-r3 自动执行链已部署。
- C 的最终值与 L2 容量以本账 + 容量—缺失曲线
  （.q4t-work/moe-residency-20260930/capacity-curve-v2/）+ 实测吞吐
  共同决定，不预设。

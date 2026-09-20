# 资源预算：容量、流量和关键路径

2026-09-20 · 本文数字为形状推导 D，不是 GPU profiler 实测。
GB=10^9 bytes；MiB=2^20 bytes；GiB=2^30 bytes。所有估算显式列范围。

## 1. 硬件资源不能当成等价的存储池

| 资源 | 规划对象 | 需要验证的约束 |
|---|---|---|
| 寄存器 | 累加器、GDN state tile、少量控制值 | 编译后寄存器数、spill、warp 活跃数 |
| shared memory | KV 双缓冲、GEMM operand tiles、归约 | 每 CTA 字节、bank conflict、barrier、occupancy |
| L1/L2 | 重用中的权重/激活/元数据 | 命中与实际读写；驻留是假设，不能当显式 SRAM 分配 |
| LPDDR5X | 权重、状态、KV、arena、CPU/I/O 缓冲 | CPU/GPU 共用物理 DRAM；容量和带宽要统一记账 |
| SSD / page cache | PLE 行与页面 | 冷/热随机读、IOPS、读放大、排队、页缓存容量 |

Thor 的 integrated GPU 与 CPU 共用 DRAM；不同 CUDA 内存类型的
缓存与一致性行为不同。H2D/D2H 不能按独立显卡 PCIe 模型估算，也不能
直接认定为零成本。[NVIDIA Tegra 文档](https://docs.nvidia.com/cuda/cuda-for-tegra-appnote/)
仅作为硬件背景；实现前查询本机 device properties 和实际内存类型。

目前未重新查询本机时钟、SM 数、每 SM 寄存器/shared 上限及 cache 大小。
设计不依赖未经本轮核验的峰值。后续以实际设备属性、ptxas 资源输出和
隔离实测填入，不能直接套用其他 Blackwell GPU 的资源/指令支持表。

## 2. 一份 token frame 有多大

| 对象 | BF16 / FP32 字节 | 说明 |
|---|---:|---|
| 窄流 x | 2560*2 = 5 KiB | 每 token |
| 四分支 R | 10240*2 = 20 KiB | 每 token；FP8 为 10 KiB，加 scales |
| 低秩 down | 320*2 = 640 B | 每 token |
| 四个写门 | 4*2 = 8 B | 若 FP32 则 16 B；需固定舍入合同 |
| 主干 trunk chunk | C*20 KiB | C=1024 为 20 MiB；C=8192 为 160 MiB |
| ping-pong R | 2*C*20 KiB | 不含 norm、GEMM scratch、PLE 和 MTP |
| 全 prompt trunk | N*20 KiB | N=262144 为 5 GiB；逐 chunk draft 消费可避免长期保存 |
| 全行 logits | C*248320*2 | C=8192 为 3.7890625 GiB |
| PLE 查询有效 payload | 16*160 = 2560 B | 每 token，FP8 |

这些是容量。读一次加写一次要另外乘流量系数，不能把容量直接当流量。
20 KiB 的 residual 容易装入片上不代表执行 GR 的权重也装得下：
每个 down 或 up 权重矩阵为 `10240*320*2 = 6.25 MiB`。
所以单 CTA 包办一个 GRRead 可能严重限制并行度；融合深度要实测。

## 3. 长期状态容量

令每序列最大可见长度 N，主干包含 36 GDN 层和 12 QSA 层。

| 对象 | 每序列容量公式 | N=262144 |
|---|---|---:|
| 主干 SSM | 36*48*128*128*4 | 108 MiB |
| GDN conv | 36*10240*3*2 | 2.109375 MiB |
| PLE conv | 10240*9*2 | 0.17578125 MiB |
| core KV | 12*N*2 KV heads*2(K,V)*256*2 | 6 GiB |
| 现有 idx_raw+idx_comp | 12*N*128*2*2 | 1.5 GiB |
| 候选 compressed index | 12*ceil(N/4)*128*2 | 0.1875 GiB |
| 候选 raw tail | 12*至多3*128*2 | 至多 9 KiB，不含 epoch/chunk scratch |

压缩表由 1.5 GiB 缩至 0.1875 GiB 是布局/生命周期推导；不是已实现
节省值。短上下文还要避免现有固定 2048-column scoring 越界：新表
尺寸、scoring extent、padding 和 mask 必须一起修改，不能只缩分配。

MTP draft 还拥有独立 QSA KV/index，不能包含在上述 12 层主干中。
同形 draft KV 在 N=262144 时为 0.5 GiB；压缩 index 为 16 MiB。
page table、MRoPE、allocator 对齐、末页 padding、路由、checkpoint、
模型权重、视觉编码器、host buffers 都未包含在上述状态表内。

多序列实际池按已分配页和状态 slot 计算；不能把 max_seq*max_len
预分配称为按需 paged allocation。初版是否采用物理分页池单独决策。

## 4. MTP 恢复预算

设验证长度 V=k+1、批量 B。仅 SSM：

- V 个完整边界 checkpoint：`B*V*108 MiB`。
- 若需要取消回到 epoch 起点，另需 `B*108 MiB` 的基点。
- 当前工作状态还占 `B*108 MiB`，不要与快照混算或漏算。
- 例 B=4、k=3：边界快照 1728 MiB，基点 432 MiB，工作状态 432 MiB。

候选 rank-one log：每 head 每位置保存 alpha 一标量、k 的 128 个
FP32 值、u 的 128 个 FP32 值。

```text
36 * 48 * (1+128+128) * 4 = 1,776,384 B
                          = 1.694091796875 MiB / 位置 / 序列
```

仅日志与边界快照的大小比约 1:63.75；这不是端到端容量或时间加速比。
日志方案仍要基点、当前状态和 conv/tail 快照，恢复需读写状态与重放。
测量必须覆盖 a=0、部分接受、全接受以及取消；不同接受分布有不同成本。

## 5. 主模型单 token 权重逻辑读取账

假设：普通 decode、默认 BF16 dense 投影；每份所需权重读一次，
每层恰选 10 个不同专家。NVFP4 计 `0.5 B/weight + 1 B/16 weights`
的 group scale，不含小 global scales 和布局 padding。

| 部分 | 公式（bytes） | 十进制 GB |
|---|---|---:|
| GDN 投影 | 36*2*2560*(10240+6144+48+48+6144) | 4.17006 |
| QSA 与 index 投影 | 12*2*2560*(12288+512+512+6144+640) | 1.23470 |
| 层内 GR mix/inject | 48*2*(2*10240*320+4*10240)*2 | 1.26616 |
| shared experts | 48*3*2560*640*2 | 0.47186 |
| routers | 48*512*2560*2 | 0.12583 |
| routed experts | 48*10*3*2560*640*(0.5+1/16) | 1.32710 |
| lm_head | 248320*2560*2 | 1.27140 |
| 合计（以上部分） | | 9.86710 |

未计 PLE 投影、末尾 GR mixer、norm、embedding lookup、状态和激活。
这不是完整权重驻留容量，也不是 DRAM 实测流量；L2 命中会减少 DRAM
读取，tile 重复加载会增加层级访问。不能用逻辑 bytes/time 超过 DRAM
峰值来宣称硬件超峰值，也不能将同步 API 时间全部归类为可消除空闲。

## 6. batching 与 prefill 的摊销模型

一批 M 个 token、第 l 层活跃专家集合 U_l：

```text
权重逻辑流量 ≈ 共同 dense 权重
              + sum_l sum_(e in U_l) expert_weight_bytes(l,e)
              + tile 重读 / padding / scale 开销
```

共同 dense 权重可以跨 token 重用；专家收益由 M_e 和路由重合决定。
独立均匀 top-10 的示意：期望不同专家数
`512*(1-(1-10/512)^M)`，M=4 时约 38.84；真实路由与 MTP 存在相关性，
必须收集直方图，不用该假设替代测量。

prefill 的 chunk-major 权重扫描次数约 `ceil(N/C)`；专家集合每 chunk
另算。较大 C 增加复用，也增加 workspace、状态中间量和不可抢占时长。
评估目标同时包含 TTFT、并发 decode p95/p99 TPOT 和峰值内存。

## 7. PLE I/O 预算

有效 payload 2560 B/token，实际请求是页面。4 KiB 页中 160 B 行可能
跨页；若 16 行各触及一个不同页，读取 64 KiB，约 25.6 倍放大；
无共享且每行跨两页的上界示例是 128 KiB。实际由地址、去重和缓存决定。

计账：unique pages、cache hits、提交/完成延迟分位数、CPU scatter、
host/device copy、转换成本、取消后无效读取、预取覆盖比例。
当前 buffered I/O 使用系统 page cache。冷热测试分开；不在共享机器
擅自 drop_caches，也不把系统页缓存当“免费且不占内存”。
本机 zero-copy、registered memory、显式复制各自独立对比；不能因
物理 DRAM 共享就跳过可见性、缓存策略及事件协议。

## 8. 峰值容量与执行时间

```text
峰值物理内存 = 唯一驻留权重（含副本/shadow）
             + 已分配 KV/index/状态页
             + 所有活跃 epoch 快照/日志
             + 同时在途 plan arenas
             + host/I/O 缓冲 + 页缓存影响 + 系统预留
```

alias 同一物理页只计一次；两个 stream 各自 scratch 必须计两次。
按时间点的同时存活集求峰值，不能把所有模块单独最大值无条件相加，
也不能无证据假设 scratch 可以全共享。

对一个执行阶段，先分别估算计算、各层级流量、指令吞吐、访存延迟
及可用并行度约束。可重叠项取约束上界，串行依赖项沿关键路径累加；
真实争用、barrier、提交开销另测。模型不能简单写成
`总FLOPs/峰值 + 总bytes/峰值`，也不能忽略不同阶段无法相互重叠。

## 9. 每份 plan 的测量单

| 类别 | 必填记录 |
|---|---|
| 身份 | 源码/二进制/模型指纹，数值模式，plan 参数，环境开关 |
| 工作量 | B、C、N、k、a 分布、M_e 直方图、Selection 长度、输出需求 |
| GPU | kernel 时间线，DRAM/L2 bytes，SM 活跃，Tensor Core，指令/stall，spill |
| CPU/I/O | enqueue 时间，GPU 空闲原因，I/O 关键路径，复制量和等待 |
| 端到端 | TTFT、TPOT 分位数、输出 tokens/s、峰值内存、无效推测量 |
| 成本解释 | 相对旧计划哪些字节/依赖消失，哪些计算/空间新增 |

GPU profiler 扰动与无 profiler 的端到端测量分开报告。

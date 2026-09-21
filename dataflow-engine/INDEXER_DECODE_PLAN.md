# 短上下文单 token 索引打分候选

2026-09-21。基线 fb49668；独立 CUDA 映射已通过完整 HTTP、分数逐位及配对时间线，本轮接受。

## 原路径事实

full_attention.cu 的短上下文分支（可见压缩组数<=max_blocks）在
seq_id 为空时使用 BF16 GEMM+IndexerReduce，在 seq_id 非空时使用
IndexerLogitsKernel。后者 grid.x=T，每 CTA 256 线程，线程循环处理
lg=threadIdx.x, threadIdx.x+256,...，最多 2048 个候选。
每候选按 h=0..n_iq-1、d=0..hd-1 顺序计算 FP32 dot，再 relu/sum/scale。

本轮只考虑已经选择 SIMT 分支的固定 T=1、n_iq=4、hd=128、
max_blocks=2048、block_off=0。不把空 seq_id 的 GEMM 路径替换成 SIMT，
两者存在 BF16 中间值与不同归约，不能将其当作等价分派。

已接受 52ca9ef 的 1K HTTP 中 IndexerLogits 累计约 351.973 ms，
约 1.380 ms/decode 步。没有本轮配对 4K 阶段计时；不能据此声称
4K 的具体瓶颈占比或预计收益。此数字仅提供选择假设的依据。

## 执行映射

候选 grid=(8,1)，每 CTA 处理连续 256 个候选组，每线程一个组：
lg=blockIdx.x*256+threadIdx.x。保留每个分数的 h/d 顺序与 FP32
累加表达式、relu、scale、可见性 mask、seq_id 偏移、输出槽布局。
不跨线程归约，不重新排列 key 或改变 top-k，同分排序仍由后续原路径负责。

1K 附近只有一至两个 CTA 有可见组，其他 CTA 只写 sentinel；
4K 附近约四个有实际计算，接近 8K 时约八个。分派可能加重短档
调度开销，也可能因更多独立 CTA 隐藏访存延迟，需逐档真实验证。
原 K 读取仍跨线程步长 hd，不声称解决了所有访存合并问题。

## 生命周期和容量

沿用调用者 logits 缓冲，没有新分配、持久缓存或 shared staging。
只读 query、compressed K、位置及 seq_id；输出每个候选分数只写一次。
query 的重复读取可能增加跨 SM 缓存流量，源代码逻辑运算数没有减少。
kernel 边界仍是打分→top-k 的可见性边界，不新增 stream/event 协议。

## 交付顺序

当前多级 top-k 验收、提交后，再接入独立 CUDA 单元，避免在原
attention device 编译单元里叠加新实现。构建后首先质量 HTTP，随后
完整五档性能；任何回退保留证据并修复/撤回，不先专项或 profile。
全部通过后再检查原/新 FP32 scores 逐位相同及生成路径调用范围，
覆盖可见组数 0/1/255/256/257/1024/2048 和不同 seq_id/位置。
完整 E2E 后用同一 4K HTTP 检查局部耗时，不能用 kernel 时间替代验收。

已实现并验收，正式五档参考整体更新；不代表完整数据流引擎。

## 后续选择阶段的静态边界

等待当前 E2E 时复核 TopkSelectKernel：可见组数<=512 时直接输出
全部位置，不执行排序；超过 512 才运行内联 2048 元素 shared bitonic
网络，按排序块序展开，并追加未完成组尾。不能将 dense 与 sparse
分支视为相同成本，也不能省略槽序或改变同分 ID。

已接受 52ca9ef 的 1K 时间线中，该 kernel 3060 次累计 10.867 ms，
约 0.043 ms/decode 步；这是 dense 分支，不能外推 4K sparse 成本。
待当前候选验收及 4K 配对时间线完成，再决定是否把精确寄存器网络
复用于短路径排序。尚未实现，不与本轮打分 CTA 映射混合验收。

验收结果：4K decode +6.20%，其余四档与 TTFT 范围重叠；288 组、
589824 个 FP32 分数逐位一致，4K 打分累计 1158.343→280.322 ms。
完整事实与边界见 [阶段报告](../docs/INDEXER_DECODE_2026-09-21.md)。

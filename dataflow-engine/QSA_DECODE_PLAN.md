# 单 token QSA 输出维度拆分候选

2026-09-21。源码推演，草稿仅在 .q4t-work/ 准备，未接入构建或运行；不是收益承诺。
前置：完成 MoE 最终二进制验收并形成独立提交。

## 依据

src/model/full_attention.cu::SparseAttentionKernel 当前 grid=(T,nkv)，
模型 nkv=2，单 token 只有两个 CTA。每 CTA 256 线程，服务 12 个 query
heads，16 个选中位置一组，串行推进全部选择；PV 每 warp 做四个
8-column tile，输出宽度 256。不能从 prefill 的大 grid 吞吐推断 decode。

首轮已过完整 E2E 的 MoE 候选 1K HTTP 时间线中，SparseAttentionKernel
255×12=3060 次、累计 924.390 ms，约 3.625 ms/decode 步。
该结果仅为选择假设的依据，来自格式整理前二进制，带 profiler 开销；
不是所有上下文的成本，更不是可回收时间的证明。

## 候选映射与不变量

单 token 将每个 kv-head 的 256 输出列分成四段，每段 64 列，grid
由两个 CTA 变为八个。每段重复计算完整 QK 与相同 online softmax，
只累加和输出自己负责的 V 列；无跨 CTA 求和或 softmax 合并。

- 同一 query、选择列表及位置顺序，QK MMA 的 k 次序不变。
- online max/sum、exp、BF16 P 舍入、逐 chunk 的 alpha rescale 不变。
- 每个输出元素 PV MMA 的 chunk 与乘加次序不变，只改变负责它的 CTA。
- gate 和最终 BF16 舍入不变，输出列互斥写入，不用 atomic。
- 多 token prefill 保留原路径，非模型固定形状保留原分派。
- 独立 CUDA 单元，避免修改原 attention 编译单元内的设备函数布局。

可将原 warp 的四个 PV tile 分别映射到四个 CTA，每 CTA 每 warp 一个
8-column tile。Q/K shared 布局沿用已接受的 padded stride；V 只载入
对应 64 列，局部 stride 可用 72 BF16 保持 16 字节对齐和原 padding。
此处是布局提案，需完整证明索引、尾块清零和 cp.async wait/barrier 生命周期。

## 成本与取舍

每个 kv-head 的 K 和 QK 被重复四次；所有 V 列总读取一次，Q 也被重复。
按每位置 K/V 各 256 BF16 的逻辑读取计算，K+V 字节由 1024 变成
2560，即 2.5 倍；这是逻辑读账，不是 DRAM 实测。
增加并行度可能缩短延迟，也可能因重复计算/缓存压力而回退。
不应将 CTA 数增加当作利用率提升的证据。

不采用沿选择列表 split-K 的局部 softmax 合并：它会改变归约次序，
而本候选要先验证保留逐元素数学顺序的执行映射。未来若采用该方向，
需独立说明数值模式，不能沿用本候选的逐位等价目标。

## 验收

必要构建后首项真实 HTTP 质量，随后同输入五档性能；MTP 关闭、单流，
每档三次、256 输出。先完整 E2E，再对照 kernel 数值与时间线。
若任一档出现可辨识回退，保留证据并修复/撤回，不先运行低层测试解释掉回退。
完整通过后专项覆盖空选择、部分尾块、2048 附近选择长度、非连续 page、
多 seq_id 切片与 gate；目标为逐位输出一致，不以改索引预算换速度。

本候选不减少 BF16 权重流量。MoE 首轮时间线里 BF16 GEMV 仍约
37.413 ms/步；不能因为此候选便于实现而把剩余权重/执行预算视为已解决。

## 草稿准备

`.q4t-work/qsa-decode-split-20260921/` 保存原 attention 源码快照及
qsa_decode.cu/.h 草稿。尚未加入 src/include/CMake，也未构建或测试。
静态核对映射为 column_base=blockIdx.z*64、每 warp 一个 8-column
PV tile；K 全宽、V 局部 64 列，尾块分别清零，原 wait/barrier 次序保留。
草稿仍须接入时审阅；这些源码约束不能替代后续 HTTP 和数值验证。

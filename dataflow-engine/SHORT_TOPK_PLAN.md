# 短路径 top-k 精确比较网络

2026-09-21。基线 4f4b8d8；完整 HTTP、精确选择与 4K 时间线通过，本轮接受。

## 目标与边界

已接受索引打分版 4K HTTP 时间线中，TopkSelectKernel 3060 次累计
226.446 ms，约 0.888 ms/decode 步；prefill 12 次累计 25.608 ms。
这些是 profiler 中的局部成本，不能换算成已证明的 E2E 收益。
1K 走 dense 直接展开，历史已接受时间线仅约 0.043 ms/步，潜力不同。

候选仅在固定 max_blocks=2048、block_topk=512 的短上下文分支使用
独立 short_topk.cu。单 token decode 与多 token prefill 使用同一
入口，tokens<=8192；多行位置各自决定 dense/sparse，不共用可见性。

原内联 shared bitonic 网络替换为已有私有 BitonicSortAscWarp<2048>，
其余数据加载、严格同分比较、排序槽序展开、尾位置追加及 -1 填充
保持原语义。dense 的全部位置展开原样保留。公共模板本身不改动。
score 数值、候选覆盖、序列状态、scratch 所有权和 kernel 数量不变。

## 容量与顺序

每行只读 2048 个 FP32 score，输出既有 max_topk 个 int32 位置与
一个长度；shared 分数/ID 数组仍合计 16384 字节/CTA。没有新分配、
跨流依赖或回读。sort 的最后 barrier 保证所有线程读取最终 shared
结果；后续注意力通过同 stream 的 kernel 边界消费输出。

寄存器与实际资源必须在完整 E2E 之后采集，不由私有模板名字推断。
多 token 行的 dense/sparse 混合会影响并行度，完整 TTFT 同时验收。

## 验收顺序

构建后首先质量 HTTP，然后五档各三次的完整性能；全部通过后才
数值与 profile。数值应从 Git 提取原 kernel，与候选实际公共入口
比较 topk 全槽和 topk_len，覆盖 511/512/513 组分支边界、2048 边界、
组尾、不同位置的多行输入、随机/同分/正负零分数，保留槽序。

4K HTTP 时间线对照已接受基线同请求，分开 prefill/decode 和调用数。
不混用 profile 速度与正式吞吐，不因 4K 目标收益接受其他档回退。
本轮只完成固定选择子链，不等于完整数据流执行计划。

验收：4K decode +0.76%，8K TTFT -1.08%，其他范围重叠；
352 组、277598448 个槽/长度逐位一致，完整事实与限制见
[阶段报告](../docs/SHORT_TOPK_2026-09-21.md)。

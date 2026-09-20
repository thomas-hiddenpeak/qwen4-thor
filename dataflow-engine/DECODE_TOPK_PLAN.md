# 长上下文 decode 多级 top-k 执行计划

2026-09-21。基线 52ca9ef；完整 HTTP、200K 时间线及精确专项已通过，本轮接受。

## 当前时间账

证据 `.q4t-work/e2e/qsa-decode-split-20260921/timeline-200k/`：已通过
完整质量与五档 E2E 的精确二进制，实际 204800 输入、256 输出，摘要
一致，采集器与服务退出 0，SQLite 完整导出。255 次 decode 前向。

| decode 项目 | 累计 ms | 每步累计 ms |
|---|---:|---:|
| BF16 GEMV | 9446.451 | 37.045 |
| QSA 输出列分片 | 1264.945 | 4.961 |
| 专家 GU+DN grouped GEMM | 1513.332 | 5.935 |
| OnePassScore | 725.642 | 2.846 |
| SliceLocalTopk | 316.038 | 1.239 |
| WindowMergeTopk | 518.349 | 2.033 |
| FinalTopkExpand | 216.357 | 0.848 |

多级选择合计 1050.744 ms，即 4.121 ms/步。三个阶段共 12240 次
kernel 启动（每步 12 个 full 层，各 1+2+1 次）。
decode 窗口 15.904 秒，GPU 空隙区间并集补集 378.605 ms；累计
kernel 时间不等于严格关键路径，profile 速度不作为正式性能参考。

prefill 窗口 167.523 秒，kernel 共 2265857；主要累计成本为稀疏
注意力 35.406 秒、GDN 31.341 秒、streaming top-k merge 19.550 秒。
本次下一候选针对 decode 分支，不能宣称会改善这些 prefill 成本。
200K decode 专家 counts 回读为 0，prefill 仍 1200 次，边界与现实现一致。

## 源码分支与候选

full_attention.cu 在组数超过 2048 且 T<=4 时，先 OnePassScore，
再 SliceLocalTopk、若干 WindowMergeTopk、FinalTopkExpand。
三个选择阶段仍调用 shared-memory BitonicSortAsc。prefill 的独立
streaming_topk.cu 已使用线程内寄存器与 warp shuffle 的等价比较网络。

候选只替换固定 2048 元素排序窗口、top-512 的三个选择阶段：

- 复用同一比较网络阶段 k/j 次序和严格大于/小于规则，同分 ID 不变。
- OnePassScore 算法、FP32 分数、causal mask 与候选范围不变。
- 每层保留 local top-512、多级窗口合并及最终扩展的相同数据布局。
- 保留 incomplete group 尾 token 追加、invalid sentinel、输出槽次序。
- 不使用近似 top-k，不缩小可见历史或 512 块预算。
- 独立 CUDA 单元封装固定选择子链，沿用调用者既有 scratch，暂不改变
  allocation、stream、分数计算或 sequence ownership。
- 非固定形状沿原路径；共享比较网络可抽为私有头文件，避免维护两套
  寄存器网络。若移动模板影响旧 streaming kernel，完整 E2E 一并验收。

## 容量和依赖

T<=4；n_groups 上界沿原 65536，首层候选容量为
ceil(n_groups/2048)*512<=16384，每 token 每个候选存 FP32 分数+int32 ID。
两组 ping-pong 区域仍沿用既有 workspace，不另加全模型持久内存。
每级 kernel 边界保证上一阶段写入可见，最终扩展只读上一层 surviving
候选。线程内 register/warp 交换减少 shared 往返，但寄存器压力和
编译变化可能抵消收益，不能从源码指令数推导实测性能。

## 验收与限制

先完成代码，必要构建后首项 HTTP 质量，再固定五档性能；全部通过
后检查原/新分值、ID、expanded token 和 topk_len 逐位一致，含同分、
正负零、空/部分可见、尾窗口与多层合并。随后核对 200K HTTP 局部耗时。
输出摘要不变不替代精确选择专项；不以 kernel 局部加速覆盖 E2E 回退。

BF16 权重读取仍占主要成本，本候选不能解决整个数据流引擎或带宽问题。
短上下文单 CTA IndexerLogits 的另一条分支单独评估，不混入本轮。

## 实现进展

新增 decode_topk.cu / decode_topk.h，固定 2048/512、T<=4 的长上下文
选择链沿用原 scratch 和 ping-pong stride。OnePassScore 仍原 kernel；
其他窗口/预算继续旧选择链。比较模板抽为私有 bitonic_topk.cuh，
streaming_topk.cu 共用，避免复制维护第二套网络。

候选源码、私有头、旧/新二进制和 patch 保存在
`.q4t-work/e2e/decode-topk-register-20260921/`。构建零警告，
第一项质量 HTTP 11/11、五档性能 15/15，通过后启动 200K HTTP 时间线。
8K/44K/200K decode 约 +2.23%/+4.03%/+4.98%，短档与 TTFT 范围重叠；
数值 64 组、2745120 个分数/ID/位置逐位一致，原 streaming 专项通过；
200K 选择链累计 1050.744→249.847 ms，正式参考整体更新，见阶段报告。

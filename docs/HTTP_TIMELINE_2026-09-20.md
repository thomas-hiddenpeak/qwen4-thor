# 已验收版本的 HTTP 时间线（2026-09-20）

29205f8 已快进合入并推送 main。随后对同一已通过五档 E2E 的二进制做
诊断采集，未修改运行时行为；模型代码仅更正一条失实的性能注释。
五档各一条真实 evalscope 请求，实际输入准确、输出均 256、结束原因
length、输出摘要与验收参考相同，服务和采集器正常退出，SQLite 导出完成。
这不是新的速度验收；正式性能仍取 POSITION_METADATA_2026-09-20.md。

## 方法与证据

- Nsight Systems 2026.1.3，交互式 launch/start/stop，模型加载完成后才采集，
  每条请求独立报告；只启用 CUDA trace，CPU sampling/context-switch 关闭。
- serve：MTP 关闭、max_seq=1、max_prefill=8192、max_len=208896；同一服务
  顺序运行五档。1K 是加载后的首请求，其余是后续请求，不混称稳态。
- 二进制 SHA-256：
  dbefb9dd794ebb1da71471d19709b3fb1dbec6415f6f8e47994b124ddb3401a8。
  tools/evalscope/profile_http.py 要求提供该二进制完整五档 HTTP 验收记录；
  性能是否已接受仍须按 EVALUATION.md 判断，脚本成功不是性能门禁的替代。
- 工具和复现命令见 tools/evalscope/README.md；原始文件保存在
  .q4t-work/e2e/http-timeline-20260920/，包括请求、响应、数据库、nsys-rep、
  SQLite、命令、摘要及 timeline-summary.json。模型与 reference/ 未写入。

阶段分界以当前 serve 代码和可核对事件为准：唯一的 496640 字节 D2H 是
prefill 最后一行 BF16 logits；此后每个 4 字节 D2H 对应 scheduler 的
argmax 结果。同时核对 EmbedLookupKernel 的 gridX 与 ArgmaxBf16ReduceKernel
计数：prefill 各块 token 数之和等于实际输入，decode 每次 gridX=1，
embedding、argmax 和 4 字节回读次数一致。分析脚本遇到形状不符会报错。

下表 prefill 窗口从请求首个 GPU 活动到 logits 回读完成，包含状态重置、
前向与期间设备空隙；decode 窗口从该回读完成到最后一个 argmax 回读结束。
这是设备时间线窗口，**不是精确的 CPU ModelPrefill 函数计时，也不是 TTFT**。
GPU busy 是 kernel、显式 memcpy、memset 区间的并集；空隙不能全部归为 CPU
提交开销。kernel 占比以累计 kernel 时间为分母，包含跨 stream 重叠，不能
与窗口百分比直接混算。CUDA API 等待与 GPU 工作重叠，不另加到总耗时上。

## 五档阶段概况

| 实际输入 | prefill 窗口秒 | prefill GPU busy | decode 窗口秒 | decode GPU busy | decode BF16 GEMV 占 kernel 累计时间 |
|---:|---:|---:|---:|---:|---:|
| 1024 | 1.114 | 87.42% | 16.748 | 93.37% | 60.41% |
| 4096 | 3.338 | 98.00% | 18.548 | 94.12% | 54.59% |
| 8192 | 6.566 | 98.79% | 17.839 | 93.74% | 57.27% |
| 45056 | 40.303 | 98.89% | 18.288 | 93.62% | 55.99% |
| 204800 | 236.620 | 99.20% | 19.129 | 94.01% | 53.39% |

同请求的 profiler decode 时段比无采集基线约长 7.8%–9.9%，所以不得把
上述时间用作新性能目标或回退判据。busy 高也不证明 SM、Tensor Core 或
LPDDR 带宽饱和；本次没有采集 DRAM 字节计数。CUPTI 显式 memcpy 不包含
kernel 读取权重/激活的流量，不能拿它来计算完整模型带宽。

## 已确认的问题与优先级

### 1. 普通生成尾部有一次无消费者的前向

五档均输出 256 token，但都有 256 次 decode embedding、argmax 与结果回读。
第一个 token 来自 prefill，理论只需 255 次后续前向。代码在最后一步设置
finish_reason=length 并发出文本后，仍注册下一次 scheduler 前向，循环退出
才丢弃其结果。该行为在 scheduler/fallback 两条普通路径均可从源码看到，
实测覆盖 scheduler；EOS 分支提前 break，不把它混入这个结论。

最后一次前向的采集窗口分别约 65.2/71.3/69.7/70.4/73.6 ms。建议首先
单独修复：达到输出上限并完成当前输出后退出，避免无用前向；对 max_tokens=1
及原五档先做真实 E2E。对 256 输出预计只是约一步的收益，不能许诺大幅
加速。采集里的毫秒数不是无 profiler 下的收益测量。本轮尚未修改此逻辑。

### 2. 200K prefill 的 top-k 合并已是大项

200K 的 MergeChunkTopkKernel 累计 63.35 秒，占 kernel 累计时间 24.59%；
SparseAttentionKernel 为 58.84 秒、22.84%，GatedDeltaNetRegKernel 为
33.56 秒、13.03%。44K 则是 sparse attention 28.16%、GDN 16.45%、
merge 7.12%。1K/4K/8K 的主导项又不同，不能以单一长度决定全部路线。

当前 merge 对每个 query/候选 chunk，先对 2048 个候选做完整 bitonic 排序，
再对 running 512 + 本块 512 做 1024 元素排序。源码注释声称流式 indexer
成本很小，与 200K 证据不符，本轮已更正；历史日志不改写。下一项较大的
长上下文研究应围绕精确 top-k 选择/归并，保持候选覆盖、排序及等分边界
行为，先真实 E2E，再检查选中索引，不预先改小召回预算或更换精度。

### 3. decode 仍有密集计算与逐层 host 往返

Bf16GevKernel 占五档 decode kernel 累计时间约 53%–60%，为最大项；
SparseAttentionKernel 约 6.7%–10.8%。这支持继续研究实际矩阵形状的执行
效率，但没有带宽计数，不能直接断言已经到 LPDDR roofline。

每档 256 次前向均有 12288 次 2048 字节专家 counts D2H，即每步 48 次，
与 MoERoutedForward 中每层 counts 回读/同步相符。另有每档 178688 次
cudaMallocAsync，即每步 698 次，涉及 MoE、linear attention 和 HC 临时量。
它们为 workspace 生命周期整理、减少 host 控制依赖提供具体依据。
API 的累计等待时间不能等同可优化时间，更不能与 GPU 时间相加；设备
空隙约 6% 也不是消除同步的完整收益上限，因为依赖会改变重叠和排队。

### 4. prefill logits 有浪费，但不是长上下文主因

服务读取末位置一行，HeadForward 仍接收整块 T 并投影为 [T,vocab]；
长上下文首块亦计算了未消费的 head。按源码位置、chunk embedding 边界
及末 kernel 的 lm_head 网格核对，相关最终投影 kernel 总计约：
1K 14.9 ms、4K 44.5 ms、8K 82.4 ms、44K 142.1 ms、200K 175.5 ms。
这里仅统计投影 kernel，不包含 mixer 等完整 head 开销。

只生成末位置 logits 值得作为复杂度/工作量简化单独处理，但 200K 下这项
投影时间远小于 top-k merge 和 sparse attention，不能用它解释长 TTFT。

## 下一阶段边界

先修复生成尾部无用前向，以 E2E 验证输出和五档性能；随后将 200K 精确
选择/归并和 decode workspace/host 控制作为不同问题推进，不混在同一改动。
当前诊断没有改变正式基线，没有重新运行整套测试或任何 bench，也没有
声称完成精确 CPU 阶段计时、生产尾延迟、并发/MTP/多模态或全面质量评估。

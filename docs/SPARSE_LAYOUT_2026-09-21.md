# 稀疏注意力 shared memory 行布局（2026-09-21）

状态：接受。必要构建无警告；构建后首测真实质量 HTTP 11/11，再五档各
三次 15/15，完整输出与上一版一致。E2E 后 442368 个 BF16 专项输出逐位
一致。200K TTFT 205.22→181.96 秒，decode 五档未见回退。
E2E 后的 200K HTTP 时间线确认稀疏注意力累计耗时减少约 39.8%，
这些带采集的计时不进入正式性能参考。

## 代码改变了什么

SparseAttentionKernel 为每个 token、每个 KV head 开一个 CTA，共享 K/V
供 12 个 query head 使用。原 Q/K/V shared 行距都是 256 个 BF16，即
512 字节；QK 与 PV 的 MMA fragment 读取不同的行时，地址容易映射到
相同 bank。原注释中的访存判断不能替代当前源码与实际测量。

本次添加 kSharedHd=264，只改变 Q/K/V 在 shared memory 的行距。每行
多 8 个 BF16（16 字节），全部读取、写入、双缓冲偏移和尾块补零使用
新行距。行起始仍满足 cp.async 的 16 字节对齐，padding 本身没有参与
运算。每 CTA 理论多 1280 字节；全局 KV 布局和容量不增加。

注意力候选、候选顺序、16-position 分块、QK、online softmax、PV、gate
和 BF16 舍入都保持原代码的数学顺序。本次没有新增分支或替代 kernel，
prefill 和 decode 共用同一实现，因此两种路径都需要验收。

## 真实 HTTP E2E

对照为已接受的 top-k 提交 6c9fbd6，不拿更早较慢版本作为本轮分母。
MTP 关闭、greedy、max_seq=1、max_prefill=8192、max_len=208896；五档
固定输入各三次，输出上限 256。无额外预热/探测、并行构建或其他由本任务
启动的推理。固定质量 11/11 精确正确，含 44K/200K；随后性能 15/15，
实际输入达标、输出 256、length、摘要一致、服务正常退出。

| 实际输入 | 旧 TTFT 秒 | 新 TTFT 秒 | 降幅 | 旧 decode | 新 decode |
|---:|---:|---:|---:|---:|---:|
| 1024 | 0.876 | 0.850 | 2.96% | 16.747 | 16.893 |
| 4096 | 3.229 | 2.851 | 11.70% | 14.908 | 15.089 |
| 8192 | 6.503 | 5.631 | 13.41% | 15.680 | 15.891 |
| 45056 | 38.307 | 33.152 | 13.46% | 15.361 | 15.511 |
| 204800 | 205.221 | 181.959 | 11.34% | 14.536 | 14.670 |

TTFT 为三次均值，含接入/分词/prefill，不是纯 prefill；decode 单位 tok/s，
按首 token 后总输出数除以对应总时间。decode 均值 +0.87%–1.35%，五档
无可辨识回退。1K/4K/8K/200K 新 decode 最低值高于旧最高值，44K 范围
略有重叠（旧 15.255–15.424，新 15.419–15.608）。不能用三次样本保证
生产流量中的稳定微小收益或尾延迟。

4K/8K/44K/200K 的每次 TTFT 均优于旧范围；200K 旧 205.063–205.322、
新 181.659–182.125 秒，均值减少 23.262 秒。1K TTFT 范围重叠，均值
减少约 26 ms；不以该小幅均值变化单独承诺稳定收益。

接受二进制 SHA-256：
394b86c5a5c7ab2557dfb45604026d5eb464bb122f7768eff926e69d15ae979b。
正式性能参考已更新，原值、旧二进制、完整请求/响应、命令、数据库和
构建/退出日志保留在 .q4t-work/e2e/sparse-padding-20260920/。
该目录沿用 9 月 20 日启动日期；验收完成于 9 月 21 日。

## E2E 后的数值专项

新增 tools/verify/compare_sparse_attention_layout.py 和 CUDA fixture。
工具要求完整质量/性能 HTTP 退出成功、同一 q4t 二进制摘要以及显式性能
接受记录。性能是否无回退仍由人工依据 E2E 数据判断，不能由退出 0 代替。

从旧提交和当前文件原样提取转换函数及 attention kernel，放入两个
namespace 独立编译，保存原文/摘要/编译命令。使用相同有限 BF16 输入，
以不同哨兵填充输出，然后检查每个结果有限且逐位一致。不是从两个
生产二进制提取机器码，也不是独立高精度 attention 数学 oracle。

覆盖 1/3/12 行，nsel=0/1/3/15/16/17/31/32/33/2048/2049/2051，
包括空集合、不足 16 的尾块、双缓冲切换、非连续物理页、单序列和
多序列 ID（与行号不同）。34 组新旧调用、442368 个有限 BF16 输出
全部逐位一致，构建无警告。详见 numerical/ 和 tools/verify/README.md。
没有前置单测、微测试、bench 或 profile，没有运行无关整套测试。

专项只检查给定候选列表下的布局兼容性；不验证 top-k 选择、模型 logits、
并发调度或多模态语义。固定 E2E 语料也不能替代全面业务质量验证。

## E2E 后的真实 HTTP 时间线与资源记录

同一二进制上补采一条 200K、256 输出的真实 evalscope HTTP 请求，
输出摘要一致，服务/采集退出 0，SQLite 导出完成。对照为上一轮 top-k
已接受版的 200K 时间线，两次均 255 次 decode 前向，prefill kernel 总
次数均 2265857，SparseAttentionKernel prefill 均 300 次。

| prefill kernel | 旧累计秒 | 新累计秒 | 新累计占比 |
|---|---:|---:|---:|
| SparseAttentionKernel | 58.842 | 35.408 | 17.27% |
| MergeChunkTopkWarpKernel | 34.085 | 34.079 | 16.62% |
| GatedDeltaNetRegKernel | 33.557 | 33.557 | 16.37% |

注意力累计减少 23.434 秒（39.83%），其他两项基本不变；GPU prefill
观测窗口从 207.541 降到 184.185 秒，支持正式 TTFT 减少 23.262 秒主要
来自本次布局改动。kernel 累计可有重叠，不等于窗口时长，也不能替代
无 profiler 的 E2E。decode 中注意力累计 1.931→1.765 秒，BF16 GEMV
仍占 kernel 累计约 53.73%，没有因本次优化消失。

Nsight/CUPTI 记录确认：注意力 staticSharedMemory 42688→43968 字节，
正好多 1280；registersPerThread 两版均 56，localMemoryPerThread 均 0，
dynamicSharedMemory 均 0。prefill/decode 两种 grid 均如此。这些资源
记录不等于实测 occupancy 或 bank 冲突数；未采集 bank 冲突硬件计数，
不能精确拆分冲突、指令调度等因素对耗时的贡献。

证据：timeline/、timeline-analysis.log、timeline-comparison.json、
attention-resources.json。长 prefill 的前三项现在约 35.4/34.1/33.6 秒，
已经接近，后续优先级必须使用新成本。下一项先盘点 decode 临时缓冲
生命周期，评估将重复分配归入持久 workspace，继续以同一 E2E 门禁接受。

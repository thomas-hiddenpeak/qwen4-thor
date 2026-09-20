# GDN 连续 q/k 读取（2026-09-21）

状态：完整 HTTP、数值核对和 200K HTTP 时间线核对完成，本次改动接受。

## 源码改动与边界

仅 GatedDeltaNetRegKernel 每 lane 连续四个 q/k BF16 的读取改成 uint2，
按原顺序解包为 FP32。原 FP32 gate 表候选已经否决并撤回，本次不添加
gate 表、准备 kernel、分配或同步。q/k 归一化、状态递归、warp 归约、
BF16 舍入、FP32 状态布局、其他 GDN 路径保持原样。

该分派只在 kd=vd=128、单序列、无 checkpoint 时使用；qkv 来自单独的
cudaMallocAsync，token stride=(2*nkh+nv)*128，head offset 是 128 的
整数倍，lane offset=lane*4。因此 uint2 读取基址及偏移满足 8 字节对齐，
每 lane 恰好读取自己原有的四个值，不跨 head 或 token 尾部。

## 完整 HTTP E2E

必要构建零警告；第一项为质量 HTTP，11/11 精确正确（含 200K）。
随后固定五档各三次、MTP 关闭、单流、greedy、输出 256，15/15 成功，
输入/输出摘要与已接受版相同，服务退出 0。没有前置专项或 profile。

对照运行时为 f189b84（与 8ac9820 相同），正式固定参考为 top-k 寄存器
网络版。TTFT 是三次均值，包含 HTTP/分词/prefill，不是纯 prefill；
decode 用首 token 后总生成量/总时间，单位 tok/s。

| 输入 | 旧 TTFT 秒 | 新 TTFT 秒 | 变化 | 旧 decode | 新 decode |
|---:|---:|---:|---:|---:|---:|
| 1024 | 0.851 | 0.836 | -1.87% | 16.963 | 16.984 |
| 4096 | 2.853 | 2.803 | -1.77% | 15.200 | 15.199 |
| 8192 | 5.626 | 5.546 | -1.42% | 15.971 | 16.000 |
| 45056 | 32.379 | 31.884 | -1.53% | 15.590 | 15.607 |
| 204800 | 167.385 | 165.127 | -1.35% | 14.703 | 14.703 |

4K/8K/44K/200K TTFT 三次均低于旧三次范围；1K 范围重叠。
五档 decode 范围均与旧范围重叠，不将小幅均值变化称为稳定提速。

候选二进制 SHA-256：
123f6fecb951893bc19621ae57b9c1b19604a39d885689698788037082886a52。

## E2E 后数值与资源核对

工具 tools/verify/compare_gdn_vector_load.py 从基线 Git 和候选源码提取
相同函数，强制要求相同二进制的完整质量/性能 HTTP 记录及性能接受标记。
覆盖 nkh/nv=1/3、16/48，ROWS=1/2/4/8，T=1/3/33/129/8192，
随机有限值、正负零、softplus 分支边界及大正负 beta 三族；每组从非零
FP32 状态开始，连续调用两次，每次消费各自上一次的状态。

240 组，1309464576 个 BF16 输出和 100270080 个 FP32 状态逐位一致，
没有非有限值。这是加载与状态更新等价性检查，不替代全面业务质量验证。
工具构建也无警告；没有运行 bench 或无关整套测试。

cuobjdump：默认 ROWS=8 旧/新 REG=72，STACK=0，LOCAL=0，SHARED=1024。
SASS 中目标函数仅一份：U16 load 静态条数从 20 减至 12，新增两条
LDG.E.64.CONSTANT，对应 q/k 原八条读取变为两条。这里是静态指令核对，
不是硬件指令计数器；不由此推出 DRAM 字节减少。

## HTTP 时间线

相同 200K 请求，输入输出匹配；采集器/服务退出 0，SQLite 完整导出。
与已接受 top-k 寄存器网络版的同请求时间线比较：

| prefill kernel | 旧累计秒 | 新累计秒 | 调用数 |
|---|---:|---:|---:|
| GatedDeltaNetRegKernel | 33.552 | 31.375 | 900→900 |
| SparseAttentionKernel | 35.406 | 35.405 | 300→300 |
| MergeChunkTopkWarpKernel | 19.549 | 19.551 | 3888→3888 |

GDN 累计减少约 6.49%（2.178 秒），其余两个主要 kernel 基本不变。
prefill 窗口 169.802→167.486 秒，kernel 次数均为 2265857；decode
前向仍为 255 次、kernel 次数均为 917235。静态指令变化与目标耗时
变化互相支持，但 kernel 累计不等于墙钟时间的严格因果分解。
没有采集 DRAM 计数器；不声称全局访存流量减少或达到带宽上限。
正式性能参考只使用无 profiler 的完整五档结果。

## 证据与后续

证据 `.q4t-work/e2e/gdn-vector-load-20260921/`：quality/、performance/、
comparison.json、numerical/、resources-before/after.log、sass-before/after.log、
timeline/。旧候选 gate 预计算证据独立保留，不混作本次实现。

本次接受并整体更新正式五档参考，旧参考已冻结；后续研究 GPU 端专家执行时，需保留
每专家 scale 约定及固定 slot 合并顺序，不把减少 host 调用当作收益证明。
